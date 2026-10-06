"""Telegram Media Downloader: long polling + безопасная загрузка через yt-dlp + fallback APIs."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import tempfile
import re
import traceback
from datetime import datetime, timezone
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
    get_spotify_track_info,
    is_spotify_track_url,
    search_music,
)
import quota


WELCOME_TEXT = (
    "<b>Media Downloader</b>\n\n"
    "Пришлите ссылку на видео или просто напишите название песни. "
    "Я предложу источник и покажу найденные треки. Управление — кнопками внизу.\n\n"
    "Доступны TikTok, YouTube, Instagram, Facebook, X, Vimeo и SoundCloud. "
    "Бесплатно: 5 файлов за 24 часа, до 25 МБ. Plus+: 99 Stars за 24 часа, до 30 файлов и 49 МБ."
)

MENU_VIDEO = "📹 Видео"
MENU_MUSIC = "🎵 Музыка"
MENU_HISTORY = "🕘 История"
MENU_SUPPORT = "⭐ Plus+"
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


def format_choices(count: int, is_tiktok: bool = False) -> InlineKeyboardMarkup:
    if is_tiktok:
        rows = [[InlineKeyboardButton(text="✨ HD без водяного знака (Plus+)", callback_data="dl:1080")],
                [InlineKeyboardButton(text="🎬 SD без водяного знака", callback_data="dl:720")],
                [InlineKeyboardButton(text="🎵 MP3", callback_data="dl:audio")]]
    else:
        rows = [[InlineKeyboardButton(text=f"Видео {q}p", callback_data=f"dl:{q}") for q in ("1080", "720", "360")],
                [InlineKeyboardButton(text="🎵 MP3", callback_data="dl:audio")]]
    if count > 1:
        rows.append([InlineKeyboardButton(text=f"📦 Все {count} ссылок, 720p", callback_data="dl:batch")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def source_choices() -> InlineKeyboardMarkup:
    spotify_label = "🟢 Spotify" if os.getenv("SPOTIFY_EXTENDED_ACCESS") == "1" else "🟢 Spotify (ссылки)"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=spotify_label, callback_data="source:spotify")],
        [InlineKeyboardButton(text="🟠 SoundCloud", callback_data="source:sc")],
        [InlineKeyboardButton(text="🔴 YouTube (музыка)", callback_data="source:yt")],
    ])


def result_page(user_id: int, page: int) -> tuple[str, InlineKeyboardMarkup]:
    results = search_results.get(user_id, [])
    total = max(1, (len(results) + 4) // 5)
    page = min(max(0, page), total - 1)
    source = search_sources.get(user_id, "")
    name = {"spotify": "Spotify", "sc": "SoundCloud", "yt": "YouTube (музыка)"}.get(source, source)
    instruction = "Нажмите на трек, чтобы скачать MP3:"
    lines = [f"🎶 <b>{escape(name)}</b> · страница {page + 1}/{total}", instruction]
    rows = []
    for index in range(page * 5, min((page + 1) * 5, len(results))):
        item = results[index]
        artist = item.get("artist") or "Исполнитель неизвестен"
        lines.append(f"{index + 1}. <b>{escape(item['title'][:90])}</b> — {escape(artist[:70])}")
        icon = "🟢" if source == "spotify" else "🎧"
        rows.append([InlineKeyboardButton(text=f"{icon} {index + 1}. {item['title'][:45]}", callback_data=f"track:{index}")])
    nav = []
    if page:
        nav.append(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"page:{page - 1}"))
    if page + 1 < total:
        nav.append(InlineKeyboardButton(text="Дальше ➡️", callback_data=f"page:{page + 1}"))
    if nav:
        rows.append(nav)
    if source != "spotify":
        rows.append([InlineKeyboardButton(text="🔎 Другой источник", callback_data="source:choose")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


PLUS_TEXT = (
    "⭐ <b>Plus+ — 99 Stars на 24 часа</b>\n\n"
    "Что входит:\n"
    "• до <b>30 успешных загрузок</b> за 24 часа после покупки;\n"
    "• видео до <b>1080p</b> и файлы до <b>49 МБ</b>;\n"
    "• пакетная загрузка до <b>10 ссылок</b> (каждый файл считается отдельно).\n\n"
    "Без Plus+: <b>5 успешных файлов</b> за 24 часа, до 720p и 25 МБ за файл. "
    "Неудачные загрузки лимит не расходуют. Это доступ на 24 часа, не безлимит."
)


def plus_button() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⭐ Купить Plus+ — 99 Stars", callback_data="buy:plus")],
        [InlineKeyboardButton(text="❤️ Добровольный донат", callback_data="show:donate")],
    ])


def reset_text(status: quota.QuotaStatus) -> str:
    remaining = max(0, int((status.resets_at - datetime.now(timezone.utc)).total_seconds()))
    hours, minutes = divmod((remaining + 59) // 60, 60)
    return f"через {hours} ч {minutes} мин"


def exhausted_text(status: quota.QuotaStatus) -> str:
    if status.is_plus:
        return ("⏳ Вы использовали все 30 загрузок Plus+. Доступ обновится "
                f"{reset_text(status)}. Можно купить ещё один Plus+ за 99 Stars.")
    return ("⏳ Вы исчерпали 5 бесплатных загрузок на 24 часа. Лимит обновится "
            f"{reset_text(status)}. Подождите или купите Plus+ за 99 Stars.\n\n" + PLUS_TEXT)


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
    details = ""
    if message.from_user and quota.configured():
        try:
            status = await asyncio.to_thread(quota.get_status, message.from_user.id)
            details = f"\n\nОсталось загрузок: <b>{status.remaining}</b>. Обновление {reset_text(status)}."
        except Exception:
            logging.exception("Не удалось прочитать лимит")
    await message.answer(WELCOME_TEXT + details, reply_markup=MENU)


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
        await show_plus(message)
        return
    raw_urls = URL_RE.findall(message.text)[:10]
    if not raw_urls:
        search_queries[message.from_user.id] = message.text.strip()[:150]
        await message.answer(f"🔎 Где искать «{escape(search_queries[message.from_user.id])}»?",
                             reply_markup=source_choices())
        return
    if len(raw_urls) == 1 and is_spotify_track_url(raw_urls[0]):
        url = raw_urls[0]
        try:
            info = await asyncio.to_thread(get_spotify_track_info, url)
        except Exception:
            info = {"title": "Трек Spotify", "artist": "", "url": url}
        title = info.get("title") or "Трек Spotify"
        artist = info.get("artist") or ""
        pending[message.from_user.id] = [url]
        buttons = [
            [InlineKeyboardButton(text="⬇️ Скачать MP3", callback_data="dl:audio")],
            [InlineKeyboardButton(text="🟢 Открыть в Spotify", url=url)],
        ]
        caption = f"🎵 <b>{escape(title)}</b>"
        if artist:
            caption += f"\nИсполнитель: <b>{escape(artist)}</b>"
        caption += "\n\n🟢 Трек найден! Нажмите кнопку ниже, чтобы скачать аудио:"
        await message.answer(caption, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
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
    if quota.configured():
        try:
            status = await asyncio.to_thread(quota.get_status, message.from_user.id)
        except Exception:
            logging.exception("Не удалось прочитать лимит")
            await message.answer("⚠️ Лимиты временно недоступны. Попробуйте позже.")
            return
        if status.remaining <= 0:
            await message.answer(exhausted_text(status), reply_markup=plus_button())
            return
    tik_tok = len(urls) == 1 and "tiktok.com" in urls[0].split("/", 3)[2]
    await message.answer(title + f"Найдено ссылок: {len(urls)}. Выберите формат:",
                         reply_markup=format_choices(len(urls), is_tiktok=tik_tok))


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


@router.message(Command("plus"))
async def show_plus(message: Message) -> None:
    details = ""
    if message.from_user and quota.configured():
        try:
            status = await asyncio.to_thread(quota.get_status, message.from_user.id)
            tier = "Plus+" if status.is_plus else "бесплатно"
            details = (f"\n\nВаш тариф: <b>{tier}</b>. Осталось: <b>{status.remaining}</b> файлов. "
                       f"Обновление {reset_text(status)}.")
        except Exception:
            logging.exception("Не удалось прочитать лимит")
            details = "\n\n⚠️ База лимитов временно недоступна."
    await message.answer(PLUS_TEXT + details, reply_markup=plus_button())


@router.callback_query(F.data == "show:donate")
async def donation_details(callback: CallbackQuery) -> None:
    await callback.answer()
    await show_donations(callback.message)


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


@router.callback_query(F.data == "buy:plus")
async def plus_invoice(callback: CallbackQuery) -> None:
    await callback.answer()
    if not quota.configured():
        await callback.message.answer("Покупка Plus+ временно недоступна: база лимитов ещё не подключена.")
        return
    if not os.getenv("PAYMENT_SUPPORT_USERNAME"):
        await callback.message.answer("Покупка Plus+ временно недоступна: контакт поддержки ещё не настроен.")
        return
    try:
        status = await asyncio.to_thread(quota.get_status, callback.from_user.id)
    except Exception:
        logging.exception("База лимитов недоступна перед платежом")
        await callback.message.answer("База лимитов временно недоступна. Платёж не создан.")
        return
    if status.is_plus and status.remaining > 0:
        await callback.message.answer(f"⭐ Plus+ уже активен: осталось {status.remaining} файлов. "
                                      f"Доступ до обновления {reset_text(status)}.")
        return
    await callback.message.answer_invoice(
        title="Plus+ на 24 часа",
        description="До 30 файлов за 24 часа, до 49 МБ, 1080p и пакетная загрузка до 10 ссылок.",
        payload="plus:v1", currency="XTR",
        prices=[LabeledPrice(label="Plus+ на 24 часа", amount=quota.PLUS_PRICE)],
    )


@router.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery) -> None:
    if query.invoice_payload == "plus:v1":
        valid = (quota.configured() and bool(os.getenv("PAYMENT_SUPPORT_USERNAME"))
                 and query.currency == "XTR" and query.total_amount == quota.PLUS_PRICE)
        if valid:
            try:
                status = await asyncio.to_thread(quota.get_status, query.from_user.id)
                if status.is_plus and status.remaining > 0:
                    valid = False
            except Exception:
                valid = False
                logging.exception("База лимитов недоступна при подтверждении платежа")
        await query.answer(ok=valid, error_message=None if valid else "Покупка временно недоступна. Попробуйте позже.")
        return
    valid = (bool(os.getenv("PAYMENT_SUPPORT_USERNAME"))
             and query.invoice_payload in {"donation:50", "donation:100", "donation:250"}
             and query.currency == "XTR"
             and query.total_amount == int(query.invoice_payload.split(":", 1)[1]))
    await query.answer(ok=valid, error_message=None if valid else "Неверные данные платежа.")


@router.message(F.successful_payment)
async def donation_received(message: Message) -> None:
    payment = message.successful_payment
    if payment.invoice_payload == "plus:v1":
        if payment.currency != "XTR" or payment.total_amount != quota.PLUS_PRICE:
            logging.error("Некорректный платёж Plus+ charge_id=%s", payment.telegram_payment_charge_id)
            await message.answer("Оплата получена, но данные платежа требуют проверки. Напишите /paysupport.")
            return
        try:
            created = await asyncio.to_thread(
                quota.grant_plus, message.from_user.id, payment.telegram_payment_charge_id,
                payment.total_amount,
            )
        except Exception:
            logging.exception("Не удалось сохранить платёж Plus+ charge_id=%s", payment.telegram_payment_charge_id)
            await message.answer("Оплата прошла, но доступ пока не активирован из-за ошибки базы. "
                                 "Сохраните квитанцию и напишите /paysupport.")
            return
        if created:
            await message.answer("⭐ Plus+ активирован на 24 часа! У вас 30 загрузок до 49 МБ, "
                                 "доступны 1080p и пакетная загрузка до 10 ссылок.", reply_markup=MENU)
        else:
            await message.answer("⭐ Этот платёж Plus+ уже учтён.", reply_markup=MENU)
        return
    logging.info("Stars donation user=%s amount=%s charge_id=%s",
                 message.from_user.id if message.from_user else None,
                 payment.total_amount, payment.telegram_payment_charge_id)
    await message.answer("❤️ Спасибо за поддержку! Сохраните квитанцию Telegram на случай вопроса по платежу.",
                         reply_markup=MENU)


@router.message(F.refunded_payment)
async def payment_refunded(message: Message) -> None:
    payment = message.refunded_payment
    if payment.invoice_payload == "plus:v1" and quota.configured():
        try:
            await asyncio.to_thread(quota.refund_plus, payment.telegram_payment_charge_id)
        except Exception:
            logging.exception("Не удалось отметить возврат Plus+ charge_id=%s",
                              payment.telegram_payment_charge_id)
        await message.answer("Возврат Stars зарегистрирован. Доступ Plus+ по этому платежу отменён.")
    else:
        await message.answer("Возврат Stars зарегистрирован.")


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
    item = results[index]
    if not item.get("downloadable", True) or is_spotify_track_url(item.get("url", "")):
        title = item.get("title", "")
        artist = item.get("artist", "")
        spotify_url = item.get("url", "")
        pending[callback.from_user.id] = [spotify_url]
        buttons = [[InlineKeyboardButton(text="⬇️ Скачать MP3", callback_data="dl:audio")]]
        if spotify_url and is_spotify_track_url(spotify_url):
            buttons.append([InlineKeyboardButton(text="🟢 Открыть в Spotify", url=spotify_url)])
        header = f"🎵 <b>{escape(title)}</b>"
        if artist:
            header += f"\nИсполнитель: <b>{escape(artist)}</b>"
        await callback.message.answer(
            f"{header}\n\n🟢 Найдено в Spotify. Нажмите кнопку ниже, чтобы скачать:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        )
        return
    pending[callback.from_user.id] = [item["url"]]
    detail = f"Источник: {escape(item['source'])}."
    await callback.message.answer(f"🎵 <b>{escape(item['title'])}</b>\n{detail}",
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
    account = None
    if quota.configured():
        try:
            account = await asyncio.to_thread(quota.get_status, user_id)
        except Exception:
            logging.exception("Не удалось проверить лимит")
            await callback.message.answer("⚠️ Лимиты временно недоступны. Попробуйте позже.")
            return
        if account.remaining <= 0:
            await callback.message.answer(exhausted_text(account), reply_markup=plus_button())
            return
        if len(targets) > (10 if account.is_plus else 3):
            await callback.message.answer("📦 Бесплатная пакетная загрузка — до 3 ссылок. "
                                          "Plus+ позволяет до 10 ссылок за раз.", reply_markup=plus_button())
            return
        if not account.is_plus and quality == "1080":
            await callback.message.answer("🎬 1080p доступно в Plus+. Бесплатно можно выбрать 720p или 360p.",
                                          reply_markup=plus_button())
            return
    active_users.add(user_id)
    status = await callback.message.answer(f"⏳ Обрабатываю {len(targets)} файл(ов)…")
    bot = callback.message.bot
    try:
        async with download_slots:
            for index, url in enumerate(targets, 1):
                try:
                    if quota.configured():
                        account = await asyncio.to_thread(quota.get_status, user_id)
                        if account.remaining <= 0:
                            await callback.message.answer(exhausted_text(account), reply_markup=plus_button())
                            break
                    max_mb = min(env_int("MAX_UPLOAD_MB", 49, 1, 49),
                                 account.max_mb if account else 49)
                    max_bytes = max_mb * 1024 * 1024

                    # Send typing/upload action so user sees progress
                    action = ChatAction.UPLOAD_VOICE if quality == "audio" else ChatAction.UPLOAD_VIDEO
                    typing_task = asyncio.create_task(
                        _keep_action(bot, callback.message.chat.id, action)
                    )

                    try:
                        with tempfile.TemporaryDirectory(prefix="telegram-media-") as tmp:
                            media = await asyncio.to_thread(download_media, url, Path(tmp), max_bytes, quality)
                            caption = f"<b>{escape(media.title[:180])}</b>\nИсточник: {escape(media.source_url[:700])}"
                            upload = FSInputFile(media.path)
                            if quality == "audio" or media.path.suffix.lower() in (".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus"):
                                thumb = FSInputFile(media.thumbnail_path) if media.thumbnail_path and media.thumbnail_path.exists() else None
                                await callback.message.answer_audio(
                                    upload,
                                    caption=caption,
                                    title=media.title,
                                    performer=media.artist or None,
                                    thumbnail=thumb,
                                    request_timeout=300,
                                )
                            elif media.path.suffix.lower() == ".mp4":
                                try:
                                    await callback.message.answer_video(upload, caption=caption, supports_streaming=True, request_timeout=300)
                                except TelegramBadRequest:
                                    await callback.message.answer_document(FSInputFile(media.path), caption=caption, request_timeout=300)
                            else:
                                await callback.message.answer_document(upload, caption=caption, request_timeout=300)
                    finally:
                        typing_task.cancel()
                        with suppress(asyncio.CancelledError):
                            await typing_task

                    if url not in history[user_id]:
                        history[user_id].appendleft(url)
                    if quota.configured():
                        account = await asyncio.to_thread(quota.record_success, user_id)
                except (FileTooLargeError, MediaDownloadError) as exc:
                    logging.warning("Download error for %s: %s", url, exc)
                    await callback.message.answer(f"❌ {index}/{len(targets)}: {escape(str(exc))}")
                except Exception:
                    logging.exception("Ошибка загрузки %s", url)
                    await callback.message.answer(
                        f"❌ {index}/{len(targets)}: внутренняя ошибка. "
                        "Попробуйте ещё раз или отправьте другую ссылку."
                    )
        with suppress(TelegramBadRequest):
            await status.delete()
        if account and quota.configured():
            await callback.message.answer(
                f"✅ Осталось загрузок: <b>{account.remaining}</b>. Обновление {reset_text(account)}.")
    finally:
        active_users.discard(user_id)


async def _keep_action(bot: Bot, chat_id: int, action: ChatAction) -> None:
    """Keep sending chat action every 4 seconds until cancelled."""
    while True:
        try:
            await bot.send_chat_action(chat_id=chat_id, action=action)
        except Exception:
            pass
        await asyncio.sleep(4)


@router.message()
async def unsupported_message(message: Message) -> None:
    await message.answer("Пришлите ссылку на видео текстовым сообщением.")


async def health(_request: web.Request) -> web.Response:
    return web.Response(text="ok")


async def main() -> None:
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("Переменная BOT_TOKEN не задана")
    if quota.configured():
        await asyncio.to_thread(quota.initialize)
        logging.info("Постоянная база лимитов подключена")
    else:
        logging.warning("DATABASE_URL не задан: лимиты и покупка Plus+ отключены")
    if not os.getenv("PAYMENT_SUPPORT_USERNAME"):
        logging.warning("PAYMENT_SUPPORT_USERNAME не задан: платежи Stars отключены")

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
                BotCommand(command="plus", description="Plus+ и лимиты"),
                BotCommand(command="donate", description="Добровольный донат"),
                BotCommand(command="paysupport", description="Помощь по платежам"),
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
