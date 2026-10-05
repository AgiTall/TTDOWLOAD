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
from aiogram.types import (BotCommand, FSInputFile, Message, CallbackQuery,
                           InlineKeyboardButton, InlineKeyboardMarkup,
                           KeyboardButton, ReplyKeyboardMarkup, LabeledPrice,
                           PreCheckoutQuery)

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
    "Пришлите ссылку на видео или просто напишите название песни. "
    "Я предложу источник и покажу найденные треки. Управление — кнопками внизу.\n\n"
    "Доступны TikTok, YouTube, Instagram, Facebook, X, Vimeo и SoundCloud. Лимит файла — 49 МБ."
)

MENU_VIDEO = "📹 Видео"
MENU_MUSIC = "🎵 Музыка"
MENU_HISTORY = "🕘 История"
MENU_SUPPORT = "⭐ Поддержать"
MENU = ReplyKeyboardMarkup(keyboard=[
    [KeyboardButton(text=MENU_VIDEO), KeyboardButton(text=MENU_MUSIC)],
    [KeyboardButton(text=MENU_HISTORY), KeyboardButton(text=MENU_SUPPORT)],
], resize_keyboard=True, is_persistent=True)

router = Router()
active_users: set[int] = set()
download_slots: asyncio.Semaphore
pending: dict[int, list[str]] = {}
search_results: dict[int, list[dict]] = {}
search_queries: dict[int, str] = {}
search_sources: dict[int, str] = {}
search_generations: dict[int, int] = defaultdict(int)
history: dict[int, deque[str]] = defaultdict(lambda: deque(maxlen=10))
URL_RE = re.compile(r"https?://[^\s<>]+", re.I)


def format_choices(count: int) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=f"Видео {q}p", callback_data=f"dl:{q}") for q in ("1080", "720", "360")],
            [InlineKeyboardButton(text="🎵 MP3", callback_data="dl:audio")]]
    if count > 1:
        rows.append([InlineKeyboardButton(text=f"📦 Все {count} ссылок, 720p", callback_data="dl:batch")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def source_choices() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🟢 Spotify", callback_data="source:spotify")],
        [InlineKeyboardButton(text="🟠 SoundCloud", callback_data="source:sc")],
        [InlineKeyboardButton(text="🔴 YouTube (музыка)", callback_data="source:yt")],
    ])


def result_page(user_id: int, page: int) -> tuple[str, InlineKeyboardMarkup]:
    results = search_results.get(user_id, [])
    total = max(1, (len(results) + 4) // 5)
    page = min(max(0, page), total - 1)
    source = search_sources.get(user_id, "")
    name = {"spotify": "Spotify", "sc": "SoundCloud", "yt": "YouTube (музыка)"}.get(source, source)
    lines = [f"🎶 <b>{escape(name)}</b> · страница {page + 1}/{total}", "Нажмите на трек, чтобы выбрать формат:"]
    rows = []
    for index in range(page * 5, min((page + 1) * 5, len(results))):
        item = results[index]
        artist = item.get("artist") or "Исполнитель неизвестен"
        lines.append(f"{index + 1}. <b>{escape(item['title'][:90])}</b> — {escape(artist[:70])}")
        rows.append([InlineKeyboardButton(text=f"🎧 {index + 1}. {item['title'][:45]}", callback_data=f"track:{index}")])
    nav = []
    if page:
        nav.append(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"page:{page - 1}"))
    if page + 1 < total:
        nav.append(InlineKeyboardButton(text="Дальше ➡️", callback_data=f"page:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="🔎 Другой источник", callback_data="source:choose")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


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
    await message.answer(WELCOME_TEXT, reply_markup=MENU)


@router.message(F.text & ~F.text.startswith("/"))
async def handle_link(message: Message) -> None:
    if not message.from_user or not message.text:
        return
    if message.text == MENU_VIDEO:
        await message.answer("Отправьте одну ссылку на видео или несколько ссылок одним сообщением.", reply_markup=MENU)
        return
    if message.text == MENU_MUSIC:
        await message.answer("Напишите название песни или пришлите ссылку на трек.", reply_markup=MENU)
        return
    if message.text == MENU_HISTORY:
        await show_history(message)
        return
    if message.text == MENU_SUPPORT:
        await show_donations(message)
        return
    raw_urls = URL_RE.findall(message.text)[:10]
    if not raw_urls:
        search_queries[message.from_user.id] = message.text.strip()[:150]
        await message.answer(f"🔎 Где искать «{escape(search_queries[message.from_user.id])}»?",
                             reply_markup=source_choices())
        return
    if len(raw_urls) == 1 and "open.spotify.com/track/" in raw_urls[0]:
        search_queries[message.from_user.id] = raw_urls[0]
        await message.answer("🎵 Ссылка на Spotify. Где искать трек?", reply_markup=source_choices())
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


@router.message(Command("donate"))
async def show_donations(message: Message) -> None:
    if not os.getenv("PAYMENT_SUPPORT_USERNAME"):
        await message.answer("Поддержка Stars появится после настройки контакта для вопросов по платежам.")
        return
    await message.answer(
        "⭐ <b>Поддержать бота</b>\nДонат добровольный и не даёт дополнительных лимитов или функций. "
        "Средства помогают оплачивать хостинг и развитие бота.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⭐ 50 Stars", callback_data="donate:50"),
             InlineKeyboardButton(text="⭐ 100 Stars", callback_data="donate:100")],
            [InlineKeyboardButton(text="⭐ 250 Stars", callback_data="donate:250")],
        ]))


@router.callback_query(F.data.startswith("donate:"))
async def donate_invoice(callback: CallbackQuery) -> None:
    await callback.answer()
    amount = int(callback.data.split(":", 1)[1])
    if amount not in {50, 100, 250}:
        return
    if not os.getenv("PAYMENT_SUPPORT_USERNAME"):
        await callback.message.answer("Платежи пока не настроены.")
        return
    await callback.message.answer_invoice(
        title="Поддержка Media Downloader",
        description="Добровольный донат на хостинг и развитие бота. Без дополнительных функций или лимитов.",
        payload=f"donation:{amount}", currency="XTR",
        prices=[LabeledPrice(label="Поддержать бота", amount=amount)],
    )


@router.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery) -> None:
    valid = (bool(os.getenv("PAYMENT_SUPPORT_USERNAME"))
             and query.invoice_payload in {"donation:50", "donation:100", "donation:250"}
             and query.currency == "XTR"
             and query.total_amount == int(query.invoice_payload.split(":", 1)[1]))
    await query.answer(ok=valid, error_message=None if valid else "Неверные данные платежа.")


@router.message(F.successful_payment)
async def donation_received(message: Message) -> None:
    payment = message.successful_payment
    logging.info("Stars donation user=%s amount=%s charge_id=%s",
                 message.from_user.id if message.from_user else None,
                 payment.total_amount, payment.telegram_payment_charge_id)
    await message.answer("❤️ Спасибо за поддержку! Сохраните квитанцию Telegram на случай вопроса по платежу.",
                         reply_markup=MENU)


@router.message(Command("paysupport"))
async def payment_support(message: Message) -> None:
    contact = os.getenv("PAYMENT_SUPPORT_USERNAME", "").strip().lstrip("@")
    if contact:
        await message.answer(f"По вопросам платежей напишите @{escape(contact)} и приложите квитанцию Telegram.")
    else:
        await message.answer("По вопросам платежей ответьте на квитанцию Telegram и опишите проблему. "
                             "Владелец бота должен настроить PAYMENT_SUPPORT_USERNAME в Render.")


@router.message(Command("music"))
async def music_search(message: Message) -> None:
    if not message.from_user:
        return
    query = (message.text or "").partition(" ")[2].strip()
    if not query:
        await message.answer("Напишите /music название песни; источник: /music spotify, /music yt или /music sc название.")
        return
    search_queries[message.from_user.id] = query[:150]
    await message.answer("Где искать?", reply_markup=source_choices())


@router.callback_query(F.data.startswith("source:"))
async def choose_source(callback: CallbackQuery) -> None:
    await callback.answer()
    user_id = callback.from_user.id
    source = callback.data.split(":", 1)[1]
    if source == "choose":
        await callback.message.edit_text("Где искать?", reply_markup=source_choices())
        return
    query = search_queries.get(user_id)
    if not query:
        await callback.message.edit_text("Поиск устарел. Напишите название песни ещё раз.")
        return
    search_generations[user_id] += 1
    generation = search_generations[user_id]
    search_sources[user_id] = source
    await callback.message.edit_text("🔎 Подключаюсь к каталогу…")
    animation = asyncio.create_task(search_animation(callback.message, generation, user_id))
    try:
        results = await asyncio.to_thread(search_music, query, source)
        if generation != search_generations[user_id]:
            return
        search_results[user_id] = [item for item in results if item["url"]][:20]
    except MediaDownloadError as exc:
        await callback.message.edit_text(f"⚠️ {escape(str(exc))}", reply_markup=source_choices())
        return
    finally:
        animation.cancel()
        with suppress(asyncio.CancelledError):
            await animation
    if not search_results[user_id]:
        await callback.message.edit_text("Ничего не найдено. Попробуйте другой источник.",
                                         reply_markup=source_choices())
        return
    text, markup = result_page(user_id, 0)
    await callback.message.edit_text(text, reply_markup=markup)


async def search_animation(message: Message, generation: int, user_id: int) -> None:
    frames = ("🔎 Ищу треки ·", "🔎 Ищу треки ··", "🔎 Ищу треки ···")
    index = 0
    while generation == search_generations[user_id]:
        await asyncio.sleep(3)
        with suppress(TelegramBadRequest):
            await message.edit_text(frames[index % len(frames)])
        index += 1


@router.callback_query(F.data.startswith("page:"))
async def turn_page(callback: CallbackQuery) -> None:
    await callback.answer()
    if not search_results.get(callback.from_user.id):
        await callback.message.edit_text("Поиск устарел. Напишите название песни ещё раз.")
        return
    page = int(callback.data.split(":", 1)[1])
    text, markup = result_page(callback.from_user.id, page)
    await callback.message.edit_text(text, reply_markup=markup)


@router.callback_query(F.data.startswith("track:"))
async def choose_track(callback: CallbackQuery) -> None:
    await callback.answer()
    results = search_results.get(callback.from_user.id, [])
    index = int(callback.data.split(":", 1)[1])
    if index >= len(results):
        await callback.message.answer("Поиск устарел. Повторите /music.")
        return
    pending[callback.from_user.id] = [results[index]["url"]]
    await callback.message.answer(f"🎵 <b>{escape(results[index]['title'])}</b>\n"
                                  "MP3 будет получен из доступного источника; Spotify предоставляет только данные трека.",
                                  reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                      [InlineKeyboardButton(text="⬇️ Скачать MP3", callback_data="dl:audio")]]))


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
