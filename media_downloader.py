"""Безопасная загрузка одного медиафайла через yt-dlp + fallback API."""

from __future__ import annotations

import re
import json
import os
import time
import shutil
import base64
import logging
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlencode, quote, urljoin
from urllib.request import Request, urlopen, build_opener, HTTPRedirectHandler
from urllib.error import HTTPError, URLError

from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError as YtDlpDownloadError

logger = logging.getLogger(__name__)

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
    "spotify.com",
    "spotify.link",
)

URL_RE = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)

# User-Agent strings for rotation
_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0",
]
_ua_index = 0


def _next_ua() -> str:
    global _ua_index
    ua = _USER_AGENTS[_ua_index % len(_USER_AGENTS)]
    _ua_index += 1
    return ua


def is_spotify_track_url(url: str) -> bool:
    parsed = urlsplit(url)
    try:
        port = parsed.port
    except ValueError:
        return False
    host = (parsed.hostname or "").lower()
    if host not in ("open.spotify.com", "spotify.link", "spotify.com") and not host.endswith(".spotify.com"):
        return False
    if parsed.username or parsed.password or port:
        return False
    return bool(re.search(r"/(?:intl-[a-zA-Z-]+/)?track/[a-zA-Z0-9]+", parsed.path))


def get_spotify_track_info(url: str) -> dict:
    """Extract track title, artist, and album art from Spotify track URL."""
    # 1. Try Spotify OpenGraph metadata
    try:
        req = Request(
            url,
            headers={
                "User-Agent": "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)",
                "Accept-Language": "en-US,en;q=0.9",
            },
        )
        with urlopen(req, timeout=10) as resp:
            html = resp.read().decode("utf-8", "ignore")
        title_m = re.search(r'<meta property="og:title" content="([^"]+)"', html)
        desc_m = re.search(r'<meta property="og:description" content="([^"]+)"', html)
        artist_m = re.search(r'<meta name="music:musician_description" content="([^"]+)"', html)
        img_m = re.search(r'<meta property="og:image" content="([^"]+)"', html)

        title = title_m.group(1).strip() if title_m else ""
        artist = artist_m.group(1).strip() if artist_m else ""
        if not artist and desc_m:
            desc = desc_m.group(1)
            artist = desc.split("·")[0].strip() if "·" in desc else desc.split(" - ")[0].strip()
        img = img_m.group(1).strip() if img_m else ""
        if title:
            return {"title": title, "artist": artist, "image": img, "url": url}
    except Exception as exc:
        logger.debug("Spotify OpenGraph fetch failed for %s: %s", url, exc)

    # 2. Fallback to Spotify oEmbed
    try:
        oembed_url = "https://open.spotify.com/oembed?url=" + quote(url, safe="")
        req = Request(oembed_url, headers={"User-Agent": _next_ua()})
        with urlopen(req, timeout=10) as resp:
            data = json.load(resp)
            title = data.get("title", "").strip()
            img = data.get("thumbnail_url", "").strip()
            if title:
                return {"title": title, "artist": "", "image": img, "url": url}
    except Exception as exc:
        logger.debug("Spotify oEmbed fetch failed for %s: %s", url, exc)

    return {"title": "Трек Spotify", "artist": "", "image": "", "url": url}


class MediaDownloadError(RuntimeError):
    """Понятная пользователю ошибка загрузки."""


class UnsupportedUrlError(MediaDownloadError):
    """Ссылка ведёт на неподдерживаемый или небезопасный адрес."""


class FileTooLargeError(MediaDownloadError):
    """Итоговый файл превышает лимит отправки."""


TIKWM_MEDIA_DOMAINS = ("tikwm.com", "tiktokcdn.com", "tiktokcdn-us.com",
                       "tiktokcdn-eu.com", "byteoversea.com", "ibytedtos.com",
                       "muscdn.com", "musemuse.cn", "bytedance.com")


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


def _download_url_to_file(url: str, path: Path, max_bytes: int, *,
                          headers: dict | None = None,
                          timeout: int = 60) -> None:
    """Download a URL to a local file with size checking."""
    hdr = {"User-Agent": _next_ua()}
    if headers:
        hdr.update(headers)
    request = Request(url, headers=hdr)
    with urlopen(request, timeout=timeout) as response:
        with path.open("wb") as output:
            total = 0
            while chunk := response.read(256 * 1024):
                total += len(chunk)
                if total > max_bytes:
                    raise FileTooLargeError(
                        "Файл превышает допустимый размер для Telegram."
                    )
                output.write(chunk)


def _is_valid_mp4(path: Path) -> bool:
    """Check if a file starts with a valid MP4 ftyp header."""
    try:
        with path.open("rb") as f:
            header = f.read(12)
        return len(header) >= 8 and header[4:8] == b"ftyp"
    except OSError:
        return False


def _tikwm_video(url: str, directory: Path, max_bytes: int, quality: str) -> DownloadedMedia:
    """Get TikWM's clean HD/SD MP4. Never fall back to a watermarked link."""
    form = urlencode({"url": url, "hd": "1"}).encode()
    request = Request("https://www.tikwm.com/api/", data=form,
                      headers={"Content-Type": "application/x-www-form-urlencoded",
                                "User-Agent": _next_ua()})
    
    # Retry TikWM API up to 3 times
    data = None
    for attempt in range(3):
        try:
            with urlopen(request, timeout=20) as response:
                payload = json.load(response)
            if isinstance(payload, dict) and payload.get("code") == 0 and isinstance(payload.get("data"), dict):
                data = payload["data"]
                break
        except (OSError, ValueError) as exc:
            logger.warning("TikWM attempt %d failed: %s", attempt + 1, exc)
            if attempt < 2:
                time.sleep(1.5 * (attempt + 1))
    
    if not data:
        raise MediaDownloadError("Не удалось получить видео TikTok без водяного знака. Попробуйте позже.")
    
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
            with opener.open(Request(media_url, headers={"User-Agent": _next_ua()}), timeout=45) as response:
                if not _allowed_tikwm_media(response.url):
                    continue
                with path.open("wb") as output:
                    total = 0
                    while chunk := response.read(256 * 1024):
                        total += len(chunk)
                        if total > max_bytes:
                            raise FileTooLargeError("Файл TikTok превышает допустимый размер для Telegram.")
                        output.write(chunk)
            if _is_valid_mp4(path):
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


def _cobalt_download(url: str, directory: Path, max_bytes: int, quality: str) -> DownloadedMedia:
    """
    Use cobalt.tools API as a fallback downloader for YouTube, Instagram, Twitter, etc.
    cobalt.tools is a free open-source service that works from cloud IPs.
    """
    cobalt_api = os.getenv("COBALT_API_URL", "https://api.cobalt.tools")

    quality_map = {
        "1080": "1080",
        "720": "720",
        "360": "360",
        "audio": "128",
    }
    
    body = {
        "url": url,
        "videoQuality": quality_map.get(quality, "720"),
        "filenameStyle": "basic",
    }
    
    if quality == "audio":
        body["downloadMode"] = "audio"
        body["audioFormat"] = "mp3"
    else:
        body["downloadMode"] = "auto"
    
    request_data = json.dumps(body).encode()
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": _next_ua(),
    }
    
    cobalt_key = os.getenv("COBALT_API_KEY", "")
    if cobalt_key:
        headers["Authorization"] = f"Api-Key {cobalt_key}"
    
    req = Request(cobalt_api + "/", data=request_data, headers=headers, method="POST")
    
    try:
        with urlopen(req, timeout=30) as response:
            result = json.load(response)
    except (OSError, ValueError) as exc:
        raise MediaDownloadError(f"Cobalt API недоступен: {exc}") from exc
    
    status = result.get("status")
    if status == "error":
        error_code = result.get("error", {}).get("code", "unknown")
        raise MediaDownloadError(f"Cobalt не смог обработать ссылку (код: {error_code}).")
    
    download_url = result.get("url")
    if not download_url:
        # Cobalt may return a 'tunnel' or 'redirect' status
        if status == "tunnel":
            download_url = result.get("url")
        elif status == "redirect":
            download_url = result.get("url")
        if not download_url:
            raise MediaDownloadError("Cobalt API не вернул ссылку на скачивание.")
    
    ext = "mp3" if quality == "audio" else "mp4"
    filename = f"cobalt_media.{ext}"
    path = directory / filename
    
    _download_url_to_file(download_url, path, max_bytes, timeout=120)
    
    if not path.exists() or path.stat().st_size == 0:
        path.unlink(missing_ok=True)
        raise MediaDownloadError("Cobalt: скачанный файл пуст.")
    
    title = result.get("filename", "Видео")
    # Clean up title
    title = re.sub(r"\.[^.]+$", "", title)[:200] or "Видео"
    
    return DownloadedMedia(path=path, title=title, source_url=url)


@dataclass(frozen=True)
class DownloadedMedia:
    path: Path
    title: str
    source_url: str
    artist: str = ""
    thumbnail_path: Path | None = None


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
    ignored_suffixes = {".part", ".ytdl", ".temp", ".json", ".txt"}
    files = [
        path
        for path in directory.iterdir()
        if path.is_file() and path.suffix.lower() not in ignored_suffixes
    ]
    if not files:
        raise MediaDownloadError("Сервис не вернул файл. Возможно, видео закрыто или удалено.")
    return max(files, key=lambda path: path.stat().st_mtime_ns)


def get_media_info(url: str) -> dict:
    opts = {
        "quiet": True,
        "noplaylist": True,
        "skip_download": True,
        "socket_timeout": 20,
        "no_warnings": True,
        "geo_bypass": True,
        "extractor_retries": 3,
    }
    # Add cookies if available
    cookies_path = os.getenv("YT_COOKIES_FILE", "")
    if cookies_path and os.path.isfile(cookies_path):
        opts["cookiefile"] = cookies_path
    
    try:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if not info:
                raise MediaDownloadError("Не удалось получить сведения о файле.")
            return info
    except YtDlpDownloadError as exc:
        raise MediaDownloadError("Не удалось прочитать ссылку. Проверьте доступность материала.") from exc


def search_music(query: str, source: str = "all") -> list[dict]:
    results = []
    original_query = query
    spotify_info = None
    if is_spotify_track_url(query):
        spotify_info = get_spotify_track_info(query)
        artist = spotify_info.get("artist") or ""
        title = spotify_info.get("title") or ""
        query = f"{artist} - {title}".strip(" -") or query
    if source == "spotify":
        if os.getenv("SPOTIFY_EXTENDED_ACCESS") != "1":
            if is_spotify_track_url(original_query):
                title = (spotify_info and spotify_info.get("title")) or "Трек Spotify"
                artist = (spotify_info and spotify_info.get("artist")) or ""
                return [{"title": title, "artist": artist,
                         "url": original_query, "source": "Spotify-ссылка",
                         "downloadable": False,
                         "image": (spotify_info and spotify_info.get("image")) or ""}]
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
            opts = {
                "quiet": True,
                "extract_flat": True,
                "noplaylist": True,
                "socket_timeout": 15,
                "no_warnings": True,
                "geo_bypass": True,
            }
            with YoutubeDL(opts) as ydl:
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


def _ytdlp_download(url: str, directory: Path, max_bytes: int, quality: str) -> DownloadedMedia:
    """Download via yt-dlp with robust options for cloud hosting."""
    max_megabytes = max(1, max_bytes // (1024 * 1024))
    height = {"1080": 1080, "720": 720, "360": 480}.get(quality)

    options = {
        "format": "bestaudio/best" if quality == "audio" else
                  f"bestvideo[height<={height}][filesize<{max_bytes}]+bestaudio/best[height<={height}][filesize<{max_bytes}]/best[height<={height}]/best",
        "outtmpl": str(directory / "%(title).80s-%(id)s.%(ext)s"),
        "noplaylist": True,
        "max_filesize": max_bytes,
        "socket_timeout": 30,
        "retries": 5,
        "fragment_retries": 5,
        "file_access_retries": 3,
        "extractor_retries": 3,
        "retry_sleep_functions": {"extractor": lambda n: 2 ** n},
        "quiet": True,
        "noprogress": True,
        "no_warnings": True,
        "windowsfilenames": True,
        "geo_bypass": True,
        "nocheckcertificate": False,
        # Important for cloud hosting: these headers help avoid blocks
        "http_headers": {
            "User-Agent": _next_ua(),
            "Accept-Language": "en-US,en;q=0.9,ru;q=0.8",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        },
    }

    # Add cookies if available (crucial for YouTube on cloud IPs)
    cookies_path = os.getenv("YT_COOKIES_FILE", "")
    if cookies_path and os.path.isfile(cookies_path):
        options["cookiefile"] = cookies_path

    if quality == "audio":
        if shutil.which("ffmpeg"):
            options["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "0"}]
        else:
            options["format"] = "bestaudio/best"
    else:
        if shutil.which("ffmpeg"):
            options["merge_output_format"] = "mp4"

    # Android/iOS client bypass for YouTube on cloud IPs
    options["extractor_args"] = {
        "youtube": {
            "player_client": ["android", "ios", "web_creator", "mweb"]
        }
    }

    try:
        with YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=True)
    except YtDlpDownloadError as exc:
        message = str(exc).lower()
        if "larger than max-filesize" in message or "max-filesize" in message:
            raise FileTooLargeError(
                f"Файл больше {max_megabytes} МБ — Telegram не сможет принять его от бота."
            ) from exc
        if "sign in" in message or "bot" in message or "captcha" in message:
            raise MediaDownloadError(
                "Источник требует подтверждение — попробуйте другое видео или подождите."
            ) from exc
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


def _download_spotify_audio(url: str, directory: Path, max_bytes: int) -> DownloadedMedia:
    """Download audio for a Spotify track by resolving it via SoundCloud or YouTube."""
    info = get_spotify_track_info(url)
    title = info.get("title") or "Трек Spotify"
    artist = info.get("artist") or ""
    query = f"{artist} - {title}".strip(" -") or title

    thumbnail_path = None
    if info.get("image"):
        try:
            thumb_file = directory / "cover.jpg"
            _download_url_to_file(info["image"], thumb_file, 5 * 1024 * 1024, timeout=15)
            if thumb_file.exists() and thumb_file.stat().st_size > 0:
                thumbnail_path = thumb_file
        except Exception as exc:
            logger.debug("Failed to download Spotify cover thumbnail: %s", exc)

    # 1. Search SoundCloud first (fastest, unblocked on cloud hosting)
    try:
        sc_tracks = search_music(query, "sc")
        if sc_tracks and sc_tracks[0].get("url"):
            try:
                media = _ytdlp_download(sc_tracks[0]["url"], directory, max_bytes, "audio")
                return DownloadedMedia(
                    path=media.path,
                    title=f"{artist} - {title}".strip(" -") if artist else title,
                    source_url=url,
                    artist=artist,
                    thumbnail_path=thumbnail_path,
                )
            except Exception as exc:
                logger.warning("SoundCloud audio download failed for %s: %s", query, exc)
    except Exception as exc:
        logger.warning("SoundCloud search failed for %s: %s", query, exc)

    # 2. Search YouTube
    try:
        yt_tracks = search_music(query, "yt")
        if yt_tracks and yt_tracks[0].get("url"):
            yt_url = yt_tracks[0]["url"]
            cobalt_disabled = os.getenv("COBALT_DISABLED", "").lower() in ("1", "true", "yes")
            if not cobalt_disabled:
                try:
                    media = _cobalt_download(yt_url, directory, max_bytes, "audio")
                    return DownloadedMedia(
                        path=media.path,
                        title=f"{artist} - {title}".strip(" -") if artist else title,
                        source_url=url,
                        artist=artist,
                        thumbnail_path=thumbnail_path,
                    )
                except Exception as exc:
                    logger.debug("Cobalt download failed for %s: %s", yt_url, exc)
            try:
                media = _ytdlp_download(yt_url, directory, max_bytes, "audio")
                return DownloadedMedia(
                    path=media.path,
                    title=f"{artist} - {title}".strip(" -") if artist else title,
                    source_url=url,
                    artist=artist,
                    thumbnail_path=thumbnail_path,
                )
            except Exception as exc:
                logger.warning("YouTube audio download failed for %s: %s", query, exc)
    except Exception as exc:
        logger.warning("YouTube search failed for %s: %s", query, exc)

    # 3. Direct yt-dlp search query fallback
    for search_prefix in (f"scsearch1:{query}", f"ytsearch1:{query}"):
        try:
            media = _ytdlp_download(search_prefix, directory, max_bytes, "audio")
            return DownloadedMedia(
                path=media.path,
                title=f"{artist} - {title}".strip(" -") if artist else title,
                source_url=url,
                artist=artist,
                thumbnail_path=thumbnail_path,
            )
        except Exception:
            continue

    raise MediaDownloadError(f"Не удалось найти и скачать аудио для трека «{title}».")


def _determine_host(url: str) -> str:
    """Extract the base domain from a URL."""
    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    return host


def download_media(url: str, directory: Path, max_bytes: int, quality: str = "720") -> DownloadedMedia:
    """Скачивает один файл; функцию следует запускать через asyncio.to_thread.
    
    Strategy:
    0. Spotify → resolve via SoundCloud/YouTube and download as audio
    1. TikTok → TikWM API (no watermark), fallback to yt-dlp
    2. YouTube → Try cobalt.tools first (works from cloud IPs), fallback to yt-dlp
    3. Instagram/Twitter/X → Try cobalt.tools first, fallback to yt-dlp
    4. Everything else → yt-dlp with retries
    """
    directory.mkdir(parents=True, exist_ok=True)

    # --- Spotify: resolve track via SoundCloud/YouTube and download audio ---
    if is_spotify_track_url(url):
        return _download_spotify_audio(url, directory, max_bytes)

    if quality not in {"1080", "720", "360", "audio"}:
        raise MediaDownloadError("Неизвестный формат файла.")

    host = _determine_host(url)
    
    # --- TikTok: use TikWM API for video (no watermark) ---
    if quality != "audio" and (host == "tiktok.com" or host.endswith(".tiktok.com")):
        try:
            return _tikwm_video(url, directory, max_bytes, quality)
        except FileTooLargeError:
            raise
        except MediaDownloadError:
            logger.warning("TikWM failed for %s, trying yt-dlp", url)
            # Fall through to yt-dlp

    # --- YouTube, Instagram, Twitter/X: try cobalt.tools first ---
    cobalt_domains = ("youtube.com", "youtu.be", "instagram.com", 
                      "twitter.com", "x.com", "facebook.com", "fb.watch",
                      "tiktok.com")
    use_cobalt = any(host == d or host.endswith("." + d) for d in cobalt_domains)
    
    if use_cobalt:
        cobalt_disabled = os.getenv("COBALT_DISABLED", "").lower() in ("1", "true", "yes")
        if not cobalt_disabled:
            try:
                return _cobalt_download(url, directory, max_bytes, quality)
            except FileTooLargeError:
                raise
            except (MediaDownloadError, Exception) as exc:
                logger.warning("Cobalt failed for %s: %s, trying yt-dlp", url, exc)

    # --- Fallback: yt-dlp ---
    candidates = [url]
    if quality == "audio" and url.startswith("scsearch1:"):
        candidates.append("ytsearch1:" + url.removeprefix("scsearch1:"))

    last_error = None
    for candidate in candidates:
        try:
            return _ytdlp_download(candidate, directory, max_bytes, quality)
        except FileTooLargeError:
            raise
        except MediaDownloadError as exc:
            last_error = exc
            logger.warning("yt-dlp failed for %s: %s", candidate, exc)
    
    raise last_error or MediaDownloadError(
        "Не удалось скачать файл. Попробуйте другую ссылку или повторите позже."
    )

