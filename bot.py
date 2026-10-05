"""Telegram Media Downloader: long polling + безопасная загрузка через yt-dlp."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import tempfile
import re
from collections import defaultdict, deque
from contextlib import suppress
from html import escape
from pathlib import Path
from aiohttp import web

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatAction, ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import BotCommand, FSInputFile, Message, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from media_downloader import (
    FileTooLargeError,
    MediaDownloadError,
    UnsupportedUrlError,
    download_media,
    extract_supported_url,
    get_media_info,
    search_music,
)


WELCOME_TEXT = (
    "<b>Media Downloader</b>\n\n"
    "Пришлите ссылку на TikTok, YouTube, Instagram, Facebook, X, Vimeo или SoundCloud. "
    "Выберите видео 1080p/720p/360p или MP3. Несколько ссылок отправьте одним сообщением.\n\n"
    "Поиск музыки: /music название (или /music yt название, /music sc название). "
    "История: /history. Очистить: /clear. Лимит файла — 49 МБ."
)

router = Router()
active_users: set[int] = set()
download_slots: asyncio.Semaphore
pending: dict[int, list[str]] = {}
search_results: dict[int, list[dict]] = {}
history: dict[int, deque[str]] = defaultdict(lambda: deque(maxlen=10))
URL_RE = re.compile(r"https?://[^\s<>]+", re.I)


def format_choices(count: int) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=f"Видео {q}p", callback_data=f"dl:{q}") for q in ("1080", "720", "360")],
            [InlineKeyboardButton(text="🎵 MP3", callback_data="dl:audio")]]
    if count > 1:
        rows.append([InlineKeyboardButton(text=f"📦 Все {count} ссылок, 720p", callback_data="dl:batch")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.getenv(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} должен быть целым числом") from exc
    if not minimum <= value <= maximum:
        raise RuntimeError(f"{name} должен быть от {minimum} до {maximum}")
    return value


@router.message(CommandStart())
@router.message(Command("help"))
async def show_help(message: Message) -> None:
    await message.answer(WELCOME_TEXT)


@router.message(F.text & ~F.text.startswith("/"))
async def handle_link(message: Message) -> None:
    if not message.from_user or not message.text:
        return
    raw_urls = URL_RE.findall(message.text)[:10]
    if not raw_urls:
        await message.answer("Пришлите ссылку или используйте /music.")
        return
    try:
        urls = [extract_supported_url(raw) for raw in raw_urls]
    except UnsupportedUrlError as exc:
        await message.answer(f"⚠️ {escape(str(exc))}")
        return
    pending[message.from_user.id] = urls
    title = ""
    if len(urls) == 1:
        try:
            info = await asyncio.to_thread(get_media_info, urls[0])
            title = f"<b>{escape(str(info.get('title') or 'Видео')[:180])}</b>\n"
            if info.get("uploader"):
                title += f"Автор: {escape(str(info['uploader'])[:80])}\n"
            if info.get("view_count"):
                title += f"Просмотров: {info['view_count']:,}\n"
        except MediaDownloadError:
            pass
    await message.answer(title + f"Найдено ссылок: {len(urls)}. Выберите формат:",
                         reply_markup=format_choices(len(urls)))


@router.message(Command("history"))
async def show_history(message: Message) -> None:
    items = list(history[message.from_user.id]) if message.from_user else []
    await message.answer("Последние ссылки:\n" + "\n".join(items) if items else "История пока пуста.")


@router.message(Command("clear"))
async def clear_history(message: Message) -> None:
    if message.from_user:
        history[message.from_user.id].clear()
    await message.answer("История очищена.")


@router.message(Command("music"))
async def music_search(message: Message) -> None:
    if not message.from_user:
        return
    query = (message.text or "").partition(" ")[2].strip()
    if not query:
        await message.answer("Напишите /music название песни; источник: /music spotify, /music yt или /music sc название.")
        return
    source = "all"
    if query.startswith(("yt ", "sc ", "spotify ")):
        source, _, query = query.partition(" ")
    status = await message.answer("🔎 Ищу треки…")
    results = await asyncio.to_thread(search_music, query[:150], source)
    results = [item for item in results if item["url"]][:8]
    search_results[message.from_user.id] = results
    if not results:
        await status.edit_text("Ничего не найдено. Попробуйте другое название.")
        return
    buttons = [[InlineKeyboardButton(text=f"{item['source']}: {item['title'][:45]}", callback_data=f"track:{i}")]
               for i, item in enumerate(results)]
    await status.edit_text("Выберите трек для MP3:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


@router.callback_query(F.data.startswith("track:"))
async def choose_track(callback: CallbackQuery) -> None:
    await callback.answer()
    results = search_results.get(callback.from_user.id, [])
    index = int(callback.data.split(":", 1)[1])
    if index >= len(results):
        await callback.message.answer("Поиск устарел. Повторите /music.")
        return
    pending[callback.from_user.id] = [results[index]["url"]]
    await callback.message.answer(escape(results[index]["title"]), reply_markup=format_choices(1))


@router.callback_query(F.data.startswith("dl:"))
async def download_choice(callback: CallbackQuery) -> None:
    await callback.answer()
    user_id = callback.from_user.id
    urls = pending.get(user_id)
    if not urls:
        await callback.message.answer("Ссылка устарела. Отправьте её снова.")
        return
    if user_id in active_users:
        await callback.message.answer("⏳ Предыдущая загрузка ещё выполняется.")
        return
    quality = callback.data.split(":", 1)[1]
    targets = urls if quality == "batch" else urls[:1]
    if quality == "batch":
        quality = "720"
    active_users.add(user_id)
    status = await callback.message.answer(f"⏳ Обрабатываю {len(targets)} файл(ов)…")
    max_bytes = env_int("MAX_UPLOAD_MB", 49, 1, 49) * 1024 * 1024
    try:
        async with download_slots:
            for index, url in enumerate(targets, 1):
                try:
                    with tempfile.TemporaryDirectory(prefix="telegram-media-") as tmp:
                        media = await asyncio.to_thread(download_media, url, Path(tmp), max_bytes, quality)
                        caption = f"<b>{escape(media.title[:180])}</b>\nИсточник: {escape(media.source_url[:700])}"
                        upload = FSInputFile(media.path)
                        if quality == "audio":
                            await callback.message.answer_audio(upload, caption=caption, request_timeout=180)
                        elif media.path.suffix.lower() == ".mp4":
                            try:
                                await callback.message.answer_video(upload, caption=caption, supports_streaming=True, request_timeout=180)
                            except TelegramBadRequest:
                                await callback.message.answer_document(FSInputFile(media.path), caption=caption, request_timeout=180)
                        else:
                            await callback.message.answer_document(upload, caption=caption, request_timeout=180)
                    if url not in history[user_id]:
                        history[user_id].appendleft(url)
                except (FileTooLargeError, MediaDownloadError) as exc:
                    await callback.message.answer(f"❌ {index}/{len(targets)}: {escape(str(exc))}")
                except Exception:
                    logging.exception("Ошибка загрузки %s", url)
                    await callback.message.answer(f"❌ {index}/{len(targets)}: внутренняя ошибка.")
        with suppress(TelegramBadRequest):
            await status.delete()
    finally:
        active_users.discard(user_id)


@router.message()
async def unsupported_message(message: Message) -> None:
    await message.answer("Пришлите ссылку на видео текстовым сообщением.")


async def health(_request: web.Request) -> web.Response:
    return web.Response(text="ok")


async def main() -> None:
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("Переменная BOT_TOKEN не задана")

    global download_slots
    download_slots = asyncio.Semaphore(
        env_int("MAX_CONCURRENT_DOWNLOADS", 2, 1, 10)
    )

    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    runner = web.AppRunner(app)
    await runner.setup()
    port = env_int("PORT", 10000, 1, 65535)
    await web.TCPSite(runner, host="0.0.0.0", port=port).start()

    dispatcher = Dispatcher()
    dispatcher.include_router(router)

    async with Bot(
        token=token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    ) as bot:
        await bot.set_my_commands(
            [
                BotCommand(command="start", description="Запустить бота"),
                BotCommand(command="help", description="Как пользоваться"),
                BotCommand(command="music", description="Поиск музыки"),
                BotCommand(command="history", description="Последние ссылки"),
                BotCommand(command="clear", description="Очистить историю"),
            ]
        )
        await bot.delete_webhook(drop_pending_updates=False)
        logging.info("Бот запущен в режиме long polling; HTTP /health порт %s", port)
        try:
            await dispatcher.start_polling(bot)
        finally:
            await runner.cleanup()


if __name__ == "__main__":
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        stream=sys.stdout,
    )
    asyncio.run(main())
