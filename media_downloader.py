"""Безопасная загрузка одного медиафайла через yt-dlp."""

from __future__ import annotations

import re
import json
import os
import base64
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlencode, quote, urljoin
from urllib.request import Request, urlopen, build_opener, HTTPRedirectHandler

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


def is_spotify_track_url(url: str) -> bool:
    parsed = urlsplit(url)
    try:
        port = parsed.port
    except ValueError:
        return False
    return (parsed.scheme == "https" and (parsed.hostname or "").lower() == "open.spotify.com"
            and parsed.path.startswith("/track/") and not parsed.username
            and not parsed.password and not port)


class MediaDownloadError(RuntimeError):
    """Понятная пользователю ошибка загрузки."""


class UnsupportedUrlError(MediaDownloadError):
    """Ссылка ведёт на неподдерживаемый или небезопасный адрес."""


class FileTooLargeError(MediaDownloadError):
    """Итоговый файл превышает лимит отправки."""


TIKWM_MEDIA_DOMAINS = ("tikwm.com", "tiktokcdn.com", "tiktokcdn-us.com",
                       "tiktokcdn-eu.com", "byteoversea.com", "ibytedtos.com")


def _allowed_tikwm_media(url: str) -> bool:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    try:
        port = parsed.port
    except ValueError:
        return False
    return (parsed.scheme == "https" and not parsed.username and not parsed.password
            and not port and any(host == domain or host.endswith("." + domain)
                             for domain in TIKWM_MEDIA_DOMAINS))


class _SafeTikwmRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _allowed_tikwm_media(newurl):
            raise MediaDownloadError("Сервис TikTok вернул небезопасный адрес файла.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _tikwm_video(url: str, directory: Path, max_bytes: int, quality: str) -> DownloadedMedia:
    """Get TikWM's clean HD/SD MP4. Never fall back to a watermarked link."""
    form = urlencode({"url": url, "hd": "1"}).encode()
    request = Request("https://www.tikwm.com/api/", data=form,
                      headers={"Content-Type": "application/x-www-form-urlencoded",
                               "User-Agent": "Mozilla/5.0"})
    try:
        with urlopen(request, timeout=15) as response:
            payload = json.load(response)
    except (OSError, ValueError) as exc:
        raise MediaDownloadError("Не удалось получить видео TikTok без водяного знака. Попробуйте позже.") from exc
    data = payload.get("data") if isinstance(payload, dict) and payload.get("code") == 0 else None
    if not isinstance(data, dict):
        raise MediaDownloadError("Видео TikTok недоступно через сервис без водяного знака.")
    if quality == "1080":
        candidates = [data.get("hdplay"), data.get("play")]
    else:
        candidates = [data.get("play"), data.get("hdplay")]
    opener = build_opener(_SafeTikwmRedirect())
    too_large = False
    for raw_link in candidates:
        if not raw_link:
            continue
        media_url = urljoin("https://www.tikwm.com/", str(raw_link))
        if not _allowed_tikwm_media(media_url):
            continue
        safe_id = re.sub(r"[^a-zA-Z0-9_-]", "", str(data.get("id") or "video"))[:32] or "video"
        path = directory / f"tiktok-{safe_id}.mp4"
        try:
            with opener.open(Request(media_url, headers={"User-Agent": "Mozilla/5.0"}), timeout=30) as response:
                if not _allowed_tikwm_media(response.url):
                    continue
                with path.open("wb") as output:
                    total = 0
                    while chunk := response.read(256 * 1024):
                        total += len(chunk)
                        if total > max_bytes:
                            raise FileTooLargeError("Файл TikTok превышает допустимый размер для Telegram.")
                        output.write(chunk)
            with path.open("rb") as downloaded:
                header = downloaded.read(12)
            if len(header) >= 8 and header[4:8] == b"ftyp":
                return DownloadedMedia(path, str(data.get("title") or "TikTok видео")[:200], url)
        except FileTooLargeError:
            path.unlink(missing_ok=True)
            too_large = True
            continue
        except (OSError, MediaDownloadError):
            pass
        path.unlink(missing_ok=True)
    if too_large:
        raise FileTooLargeError("Даже доступная версия TikTok без водяного знака превышает лимит файла.")
    raise MediaDownloadError("Не удалось скачать версию TikTok без водяного знака. Попробуйте позже.")


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
    original_query = query
    spotify_link_title = None
    if is_spotify_track_url(query):
        try:
            request = Request("https://open.spotify.com/oembed?url=" + quote(query, safe=""),
                              headers={"User-Agent": "Mozilla/5.0"})
            with urlopen(request, timeout=10) as response:
                spotify_link_title = json.load(response).get("title")
                query = spotify_link_title or query
        except (OSError, ValueError):
            pass
    if source == "spotify":
        if os.getenv("SPOTIFY_EXTENDED_ACCESS") != "1":
            if is_spotify_track_url(original_query):
                return [{"title": spotify_link_title or "Трек Spotify", "artist": "",
                         "url": original_query, "source": "Spotify-ссылка",
                         "downloadable": False}]
            raise MediaDownloadError(
                "Поиск по каталогу Spotify недоступен для публичного бота без одобренного "
                "Extended Quota Mode. Ссылку Spotify можно прислать напрямую."
            )
        client_id = os.getenv("SPOTIFY_CLIENT_ID")
        client_secret = os.getenv("SPOTIFY_CLIENT_SECRET")
        if not client_id or not client_secret:
            raise MediaDownloadError("Поиск Spotify пока не настроен: нужны SPOTIFY_CLIENT_ID и SPOTIFY_CLIENT_SECRET в Render.")
        try:
            credentials = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
            auth = Request("https://accounts.spotify.com/api/token",
                           data=b"grant_type=client_credentials",
                           headers={"Authorization": f"Basic {credentials}",
                                    "Content-Type": "application/x-www-form-urlencoded"})
            with urlopen(auth, timeout=10) as response:
                access_token = json.load(response)["access_token"]
            tracks = []
            for offset in (0, 10):
                params = urlencode({"q": query, "type": "track", "limit": 10, "offset": offset})
                request = Request("https://api.spotify.com/v1/search?" + params,
                                  headers={"Authorization": f"Bearer {access_token}"})
                with urlopen(request, timeout=15) as response:
                    batch = json.load(response).get("tracks", {}).get("items", [])
                tracks.extend(batch)
                if len(batch) < 10:
                    break
            for track in tracks:
                title = track.get("name") or query
                artist = ", ".join(a.get("name", "") for a in track.get("artists", []))
                results.append({"title": title, "artist": artist,
                                "url": track.get("external_urls", {}).get("spotify", ""),
                                "source": "Spotify", "downloadable": False})
        except (OSError, ValueError, KeyError) as exc:
            raise MediaDownloadError("Не удалось выполнить поиск Spotify. Проверьте ключи приложения и попробуйте позже.") from exc
        return results
    sources = ("yt", "sc") if source == "all" else (source,)
    for item in sources:
        if item not in {"yt", "sc"}:
            continue
        try:
            with YoutubeDL({"quiet": True, "extract_flat": True, "noplaylist": True,
                            "socket_timeout": 15}) as ydl:
                search_query = query
                if item == "yt":
                    search_query += " песня музыка" if re.search(r"[а-яё]", query, re.I) else " song music"
                data = ydl.extract_info(f"{item}search20:{search_query}", download=False)
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
    if source == "yt":
        return rank_music_results(query, results)
    return results


def rank_music_results(query: str, results: list[dict]) -> list[dict]:
    """Prefer likely tracks and drop TV episodes from broad YouTube searches."""
    words = [word for word in re.findall(r"[\w]+", query.casefold()) if len(word) > 2]
    ranked = []
    for index, item in enumerate(results):
        title = str(item.get("title") or "").casefold()
        artist = str(item.get("artist") or "").casefold()
        text = title + " " + artist
        if words and not any(word in text for word in words):
            continue
        if re.search(r"\b(серия|сезон|выпуск|шоу|телешоу|тнт|episode|season)\b", text):
            continue
        score = sum(2 for word in words if word in title) + sum(word in artist for word in words)
        if query.casefold() in title:
            score += 3
        if re.search(r"\b(official|audio|lyrics|music|песня|трек|remix|клип)\b", text):
            score += 2
        ranked.append((-score, index, item))
    ranked.sort()
    return [item for _, _, item in ranked]


def download_media(url: str, directory: Path, max_bytes: int, quality: str = "720") -> DownloadedMedia:
    """Скачивает один файл; функцию следует запускать через asyncio.to_thread."""
    directory.mkdir(parents=True, exist_ok=True)
    max_megabytes = max(1, max_bytes // (1024 * 1024))

    if quality not in {"1080", "720", "360", "audio"}:
        raise MediaDownloadError("Неизвестный формат файла.")
    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    if quality != "audio" and (host == "tiktok.com" or host.endswith(".tiktok.com")):
        return _tikwm_video(url, directory, max_bytes, quality)
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

    candidates = [url]
    if quality == "audio" and url.startswith("scsearch1:"):
        candidates.append("ytsearch1:" + url.removeprefix("scsearch1:"))
    info = None
    for candidate in candidates:
        try:
            with YoutubeDL(options) as ydl:
                info = ydl.extract_info(candidate, download=True)
            if info:
                break
        except YtDlpDownloadError as exc:
            message = str(exc)
            if "larger than max-filesize" in message.lower() or "max-filesize" in message.lower():
                raise FileTooLargeError(
                    f"Файл больше {max_megabytes} МБ — Telegram не сможет принять его от бота."
                ) from exc
            if candidate == candidates[-1]:
                raise MediaDownloadError(
                    "Не удалось скачать файл. Проверьте, что он доступен без входа в аккаунт."
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
