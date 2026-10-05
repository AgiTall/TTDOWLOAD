"""Безопасная загрузка одного медиафайла через yt-dlp."""

from __future__ import annotations

import re
import json
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlencode, quote
from urllib.request import Request, urlopen

from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError as YtDlpDownloadError


SUPPORTED_DOMAINS = (
    "tiktok.com",
    "youtube.com",
    "youtu.be",
    "instagram.com",
    "facebook.com",
    "fb.watch",
    "twitter.com",
    "x.com",
    "vimeo.com",
    "soundcloud.com",
)

URL_RE = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)


class MediaDownloadError(RuntimeError):
    """Понятная пользователю ошибка загрузки."""


class UnsupportedUrlError(MediaDownloadError):
    """Ссылка ведёт на неподдерживаемый или небезопасный адрес."""


class FileTooLargeError(MediaDownloadError):
    """Итоговый файл превышает лимит отправки."""


@dataclass(frozen=True)
class DownloadedMedia:
    path: Path
    title: str
    source_url: str


def extract_supported_url(text: str) -> str:
    """Извлекает первую разрешённую HTTP(S)-ссылку из сообщения."""
    match = URL_RE.search(text or "")
    if not match:
        raise UnsupportedUrlError("Отправьте ссылку на видео.")

    url = match.group(0).rstrip(".,!?;:)]}'\"")
    if len(url) > 2048:
        raise UnsupportedUrlError("Ссылка слишком длинная.")

    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower().rstrip(".")

    if parsed.scheme not in {"http", "https"} or not host:
        raise UnsupportedUrlError("Ссылка имеет неверный формат.")
    try:
        port = parsed.port
    except ValueError as exc:
        raise UnsupportedUrlError("Ссылка содержит неверный порт.") from exc
    if parsed.username or parsed.password or port:
        raise UnsupportedUrlError("Ссылка с логином или нестандартным портом не поддерживается.")
    if not any(host == domain or host.endswith(f".{domain}") for domain in SUPPORTED_DOMAINS):
        services = ", ".join(SUPPORTED_DOMAINS)
        raise UnsupportedUrlError(f"Этот сайт не поддерживается. Доступны: {services}.")

    return url


def _find_downloaded_file(directory: Path) -> Path:
    ignored_suffixes = {".part", ".ytdl", ".temp"}
    files = [
        path
        for path in directory.iterdir()
        if path.is_file() and path.suffix.lower() not in ignored_suffixes
    ]
    if not files:
        raise MediaDownloadError("Сервис не вернул файл. Возможно, видео закрыто или удалено.")
    return max(files, key=lambda path: path.stat().st_mtime_ns)


def get_media_info(url: str) -> dict:
    try:
        with YoutubeDL({"quiet": True, "noplaylist": True, "skip_download": True,
                        "socket_timeout": 20}) as ydl:
            info = ydl.extract_info(url, download=False)
            if not info:
                raise MediaDownloadError("Не удалось получить сведения о файле.")
            return info
    except YtDlpDownloadError as exc:
        raise MediaDownloadError("Не удалось прочитать ссылку. Проверьте доступность материала.") from exc


def search_music(query: str, source: str = "all") -> list[dict]:
    results = []
    if "open.spotify.com/track/" in query:
        try:
            request = Request("https://open.spotify.com/oembed?url=" + quote(query, safe=""),
                              headers={"User-Agent": "Mozilla/5.0"})
            with urlopen(request, timeout=10) as response:
                query = json.load(response).get("title") or query
        except (OSError, ValueError):
            pass
    if source in {"all", "spotify"}:
        try:
            params = urlencode({"term": query, "entity": "song", "limit": 4})
            request = Request("https://itunes.apple.com/search?" + params,
                              headers={"User-Agent": "Mozilla/5.0"})
            with urlopen(request, timeout=10) as response:
                tracks = json.load(response).get("results", [])
            for track in tracks:
                title = track.get("trackName") or query
                artist = track.get("artistName") or ""
                results.append({"title": title, "artist": artist,
                                "url": f"scsearch1:{artist} {title}", "source": "Каталог музыки"})
        except (OSError, ValueError):
            pass
    if source == "spotify":
        return results
    sources = ("yt", "sc") if source == "all" else (source,)
    for item in sources:
        if item not in {"yt", "sc"}:
            continue
        try:
            with YoutubeDL({"quiet": True, "extract_flat": True, "noplaylist": True,
                            "socket_timeout": 15}) as ydl:
                data = ydl.extract_info(f"{item}search4:{query}", download=False)
                for entry in (data or {}).get("entries", []):
                    if entry:
                        target = entry.get("webpage_url") or entry.get("url") or ""
                        if item == "yt" and target and not target.startswith("http"):
                            target = "https://www.youtube.com/watch?v=" + target
                        if item == "sc" and target and not target.startswith("http"):
                            continue
                        results.append({"title": entry.get("title") or query,
                                        "artist": entry.get("uploader") or entry.get("channel") or "",
                                        "url": target,
                                        "source": "YouTube" if item == "yt" else "SoundCloud"})
        except YtDlpDownloadError:
            continue
    return results


def download_media(url: str, directory: Path, max_bytes: int, quality: str = "720") -> DownloadedMedia:
    """Скачивает один файл; функцию следует запускать через asyncio.to_thread."""
    directory.mkdir(parents=True, exist_ok=True)
    max_megabytes = max(1, max_bytes // (1024 * 1024))

    if quality not in {"1080", "720", "360", "audio"}:
        raise MediaDownloadError("Неизвестный формат файла.")
    height = {"1080": 1080, "720": 720, "360": 480}.get(quality)
    options = {
        "format": "bestaudio/best" if quality == "audio" else
                  f"bestvideo[height<={height}]+bestaudio/best[height<={height}]/best",
        "outtmpl": str(directory / "%(title).80s-%(id)s.%(ext)s"),
        "noplaylist": True,
        "max_filesize": max_bytes,
        "socket_timeout": 20,
        "retries": 2,
        "fragment_retries": 2,
        "quiet": True,
        "noprogress": True,
        "no_warnings": True,
        "windowsfilenames": True,
    }
    if quality == "audio":
        options["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "0"}]
    else:
        options["merge_output_format"] = "mp4"

    try:
        with YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=True)
    except YtDlpDownloadError as exc:
        message = str(exc)
        if "larger than max-filesize" in message.lower() or "max-filesize" in message.lower():
            raise FileTooLargeError(
                f"Видео больше {max_megabytes} МБ — Telegram не сможет принять его от бота."
            ) from exc
        raise MediaDownloadError(
            "Не удалось скачать видео. Проверьте, что оно доступно без входа в аккаунт."
        ) from exc

    if not info:
        raise MediaDownloadError("Не удалось получить сведения о видео.")

    path = _find_downloaded_file(directory)
    if path.stat().st_size > max_bytes:
        path.unlink(missing_ok=True)
        raise FileTooLargeError(
            f"Видео больше {max_megabytes} МБ — Telegram не сможет принять его от бота."
        )

    return DownloadedMedia(
        path=path,
        title=str(info.get("title") or "Видео")[:200],
        source_url=str(info.get("webpage_url") or url),
    )
