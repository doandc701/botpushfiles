"""Telegram bot: paste a link or send a file and it gets uploaded to Telegram."""

import asyncio
import copy
import html as htmllib
import json
import logging
import os
import random
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from contextlib import nullcontext, suppress
from email.message import Message
from pathlib import Path
from urllib.parse import unquote, urlparse

import httpx
import imageio_ffmpeg
import yt_dlp
from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, MessageEntity, Update
from telegram import Message as TgMessage
from telegram.constants import ChatAction
from telegram.error import Conflict, TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telethon import TelegramClient
from telethon.tl.functions.upload import SaveBigFilePartRequest, SaveFilePartRequest
from telethon.tl.types import (
    DocumentAttributeAudio,
    DocumentAttributeFilename,
    DocumentAttributeVideo,
    InputFile,
    InputFileBig,
)

# Folder with .env, downloads and the MTProto session: the project folder, or the one the settings app passes.
HOME = Path(os.getenv("BOTPUSHFILES_HOME") or Path(__file__).resolve().parent)
load_dotenv(HOME / ".env")

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telethon").setLevel(logging.WARNING)
log = logging.getLogger("botpushfiles")

BOT_TOKEN = os.environ["BOT_TOKEN"]
# Where uploads go (channel/group id like -100123..., or @channelname). Empty = reply in the same chat.
TARGET_CHAT_ID = os.getenv("TARGET_CHAT_ID", "").strip() or None
ALLOWED_USER_IDS = {int(x) for x in os.getenv("ALLOWED_USER_IDS", "").replace(" ", "").split(",") if x}
# Optional self-hosted Bot API server (https://github.com/tdlib/telegram-bot-api) raises the limit to 2000 MB.
LOCAL_BOT_API_URL = os.getenv("LOCAL_BOT_API_URL", "").rstrip("/")
# Upload over MTProto (as the bot, via Telethon) when api_id/api_hash are set: Telegram acknowledges every
# part, which gives real upload progress, and files up to 2000 MB work without a local Bot API server.
API_ID = int(os.getenv("TELEGRAM_API_ID") or 0)
API_HASH = os.getenv("TELEGRAM_API_HASH", "").strip()
MTPROTO_UPLOAD = bool(API_ID and API_HASH) and os.getenv("MTPROTO_UPLOAD", "1") != "0"
MAX_SIZE_MB = int(os.getenv("MAX_SIZE_MB") or (2000 if LOCAL_BOT_API_URL or MTPROTO_UPLOAD else 50))
MAX_SIZE = MAX_SIZE_MB * 1024 * 1024
# Optional proxy for downloads, e.g. http://host:8080 or socks5://host:1080
PROXY = os.getenv("PROXY", "").strip() or None
# Netscape-format cookies of a logged-in browser, for posts that need an account (age-restricted TikToks...).
COOKIES_FILE = Path(os.getenv("COOKIES_FILE", "").strip() or HOME / "cookies.txt")
DOWNLOAD_DIR = HOME / "downloads"
# Songs already uploaded, "<target>|nct:<song key>" -> message id, so playlists skip what the channel has.
UPLOADED_PATH = HOME / "uploaded.json"
# System ffmpeg if installed, otherwise the binary bundled with imageio-ffmpeg.
FFMPEG = shutil.which("ffmpeg") or imageio_ffmpeg.get_ffmpeg_exe()

URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
# NhacCuaTui song pages: /song/<key> and the older /bai-hat/<slug>.<key>.html. yt-dlp has no extractor for them.
NCT_RE = re.compile(r"^https?://(?:www\.|m\.)?nhaccuatui\.com/(?:song/([\w-]+)|bai-hat/[^/?#]*\.([\w-]+)\.html)", re.I)
NCT_PLAYLIST_RE = re.compile(
    r"^https?://(?:www\.|m\.)?nhaccuatui\.com/playlist/(?:[^/?#]*\.([\w-]+)\.html|([\w-]+))(?:[/?#]|$)", re.I
)
# NhacCuaTui qualities from best to worst that Telegram plays in its music player; FLAC would arrive as a document.
NCT_QUALITIES = ("320", "128")
# nhac.vn song pages: /bai-hat/<slug>-<key>. yt-dlp's generic extractor picks up an ad jingle there, not the song.
NHACVN_RE = re.compile(r"^https?://(?:www\.|m\.)?nhac\.vn/bai-hat/[\w-]*-([A-Za-z0-9]+)/?(?:[?#]|$)", re.I)
NHACVN_QUALITIES = ("320K", "128K")  # Labels of the player's sources, best first
VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".webm", ".mkv"}
AUDIO_EXTS = {".mp3", ".m4a", ".aac", ".ogg", ".opus", ".flac", ".wav"}
# Telegram only shows .mp3 / .m4a in its music player; other audio files are sent as documents.
PLAYER_AUDIO_EXTS = {".mp3", ".m4a"}
# Container for an audio stream copied out of a video without re-encoding, by codec.
AUDIO_CONTAINERS = {
    "mp4a": ".m4a", "aac": ".m4a", "alac": ".m4a", "mp3": ".mp3", "opus": ".opus", "vorbis": ".ogg", "flac": ".flac"
}
# Seconds between progress edits of the status message (Telegram rate-limits message edits).
PROGRESS_INTERVAL = 3
# Only one machine can poll a bot token. When another machine starts this bot, Telegram ends our getUpdates
# with Conflict and we exit so the newest machine keeps the bot. A fresh start ignores Conflict for this long,
# so the machine that just started is the one that stays.
TAKEOVER_GRACE = 60
STARTED_AT = time.monotonic()
# Format menus waiting for a choice that are remembered; older ones answer "link expired".
MAX_PENDING = 200
PLAYLIST_PAGE_SIZE = 10  # Songs per page of the playlist menu (Telegram allows ~100 buttons per message).
# Retries when a site refuses yt-dlp's page request with 403. TikTok does so for 40-50% of requests at random,
# sometimes several in a row; waiting longer doesn't help, so retry often (all 8 failing is ~0.4%).
PAGE_403_RE = re.compile(r"Unable to download webpage: HTTP Error 403")
PAGE_403_ATTEMPTS = 8
PAGE_403_DELAY = 1
UPLOAD_PART_SIZE = 512 * 1024  # The maximum MTProto allows.
UPLOAD_PARALLEL = 8  # Parts in flight at once, so the upload isn't bound by round-trip latency.

# Logged-in MTProto client, set by start_mtproto(); None means uploads go through the Bot API.
mtproto: TelegramClient | None = None

# Choices offered for video links: callback key -> (button label, yt-dlp options)
FORMATS = {
    "mp4": (
        "🎬 MP4",
        {
            "format": f"bv*[filesize<?{MAX_SIZE}]+ba/b[filesize<?{MAX_SIZE}]/bv*+ba/b",
            "merge_output_format": "mp4",
            "postprocessors": [{"key": "FFmpegVideoRemuxer", "preferedformat": "mp4"}],
        },
    ),
    "mp4low": (
        "📱 MP4 360p",
        {
            "format": "bv*[height<=360]+ba/b[height<=360]/wv*+ba/w",
            "merge_output_format": "mp4",
            "postprocessors": [{"key": "FFmpegVideoRemuxer", "preferedformat": "mp4"}],
        },
    ),
    # Best audio stream as-is: extract_audio() copies it out without re-encoding, so quality is unchanged.
    # TikTok also lists the post's background sound as format "audio": a different track (often an ad jingle)
    # than the video's own soundtrack, so take the video's audio and keep "audio" for slideshows that have no video.
    "audio": ("🎵 Audio gốc", {"format": "ba[format_id!=audio]/b/ba"}),
}


class TooLarge(Exception):
    pass


def is_allowed(update: Update) -> bool:
    user = update.effective_user
    return not ALLOWED_USER_IDS or (user is not None and user.id in ALLOWED_USER_IDS)


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def human_time(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02}:{s:02}" if h else f"{m}:{s:02}"


def transfer_text(title: str, done: int, total: int | None, speed: float | None, eta: float | None) -> str:
    lines = [title[:100]]
    if total:
        filled = min(12, int(12 * done / total))
        lines.append(f"{'█' * filled}{'░' * (12 - filled)} {min(done / total, 1):.0%}")
    stats = [human_size(done) + (f" / {human_size(total)}" if total else "")]
    if speed:
        stats.append(f"{human_size(speed)}/s")
    if eta is not None:
        stats.append(f"còn {human_time(eta)}")
    lines.append(" · ".join(stats))
    return "\n".join(lines)


class Progress:
    """Edits a status message, throttled; update() may be called from worker threads."""

    def __init__(self, status, loop: asyncio.AbstractEventLoop, prefix: str = "", markup=None):
        self.status = status
        self.loop = loop
        self.prefix = prefix  # Shown above every text, e.g. "bài 3/20".
        self.markup = markup  # Keyboard kept on every edit (an edit without one removes it).
        self.lock = asyncio.Lock()
        self.seq = 0
        self.last = 0.0
        self.shown = None

    def update(self, text: str, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self.last < PROGRESS_INTERVAL:
            return
        self.last = now
        self.seq += 1
        asyncio.run_coroutine_threadsafe(self._edit(text, self.seq), self.loop)

    async def set(self, text: str) -> None:
        self.seq += 1
        await self._edit(text, self.seq)

    async def _edit(self, text: str, seq: int) -> None:
        async with self.lock:
            # Skip edits that a newer one has already superseded.
            text = self.prefix + text
            if seq != self.seq or text == self.shown:
                return
            try:
                await self.status.edit_text(text, disable_web_page_preview=True, reply_markup=self.markup)
                self.shown = text
            except TelegramError as e:
                log.debug("status edit failed: %r", e)


def extract_urls(message: TgMessage) -> list[str]:
    """Links in a message as Telegram detected them (so "www.x.com/..." counts), plus hidden text links."""
    found = []
    for text, entities in ((message.text, message.entities), (message.caption, message.caption_entities)):
        if not text:
            continue
        for entity in entities or ():
            if entity.type == MessageEntity.TEXT_LINK:
                found.append(entity.url)
            elif entity.type == MessageEntity.URL:
                # Offsets count UTF-16 code units, which matters when emoji come before the link.
                utf16 = text.encode("utf-16-le")
                found.append(utf16[2 * entity.offset : 2 * (entity.offset + entity.length)].decode("utf-16-le"))
        found += URL_RE.findall(text)
    urls = [normalize_url(u if re.match(r"https?://", u, re.I) else f"https://{u}") for u in found]
    return list(dict.fromkeys(urls))  # De-duplicate, keeping order.


# Hosts that yt-dlp doesn't recognise but that serve the same pages as a host it does.
# Facebook's "Share" button sometimes gives www.prod.facebook.com links (and redirects keep that host).
HOST_ALIASES = re.compile(r"^((?:[\w-]+\.)*prod\.facebook\.com)$", re.I)


def normalize_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.hostname and HOST_ALIASES.match(parsed.hostname):
        return parsed._replace(netloc="www.facebook.com").geturl()
    return url


def safe_name(name: str | None) -> str | None:
    """The last path component of a server-supplied name, or None if it is empty or a "." / ".." entry."""
    name = Path(name or "").name.strip()
    return name if name not in ("", ".", "..") else None


def guess_filename(response: httpx.Response) -> str:
    cd = response.headers.get("content-disposition")
    if cd:
        msg = Message()
        msg["content-disposition"] = cd
        if name := safe_name(msg.get_filename()):
            return name
    return safe_name(unquote(urlparse(str(response.url)).path)) or "file"


def http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(follow_redirects=True, timeout=httpx.Timeout(60, read=300), proxy=PROXY)


async def classify_link(url: str) -> tuple[str, dict | None]:
    """("video", info) gets the format menu, ("audio", info) is an audio-only page, ("file", None) a direct file.

    info is yt-dlp's unprocessed extraction result (None for direct files), reused to compute sizes.
    """
    try:
        async with http_client() as client:
            async with client.stream("GET", url, headers={"User-Agent": "Mozilla/5.0"}) as resp:
                ctype = resp.headers.get("content-type", "")
                ext = Path(urlparse(str(resp.url)).path).suffix.lower()
    except httpx.HTTPError as e:
        # Some networks drop httpx's TLS handshake to TikTok/YouTube while yt-dlp still gets through.
        try:
            info = await asyncio.to_thread(extract_info, url)
        except yt_dlp.utils.DownloadError:
            raise e from None
        return ("audio" if is_audio_only(info) else "video"), info
    if ctype.startswith("video/") or ext in VIDEO_EXTS:
        return "video", None
    if "text/html" not in ctype:
        return "file", None  # Direct audio and other files are uploaded untouched.
    info = await asyncio.to_thread(extract_info, url)
    return ("audio" if is_audio_only(info) else "video"), info


def ytdlp_extract(opts: dict, url: str, **kwargs) -> dict:
    """YoutubeDL.extract_info, retried when the site refuses its page with 403.

    TikTok answers about half of the page requests from yt-dlp with 403 at random, whatever browser it
    impersonates; asking again usually works. Only the page fetch fails this way, so nothing was downloaded yet.
    """
    for attempt in range(1, PAGE_403_ATTEMPTS + 1):
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.extract_info(url, **kwargs)
        except yt_dlp.utils.DownloadError as e:
            if attempt == PAGE_403_ATTEMPTS or not PAGE_403_RE.search(str(e)):
                raise
            log.info("page refused with 403 for %s, retrying (%d/%d)", url, attempt, PAGE_403_ATTEMPTS)
            time.sleep(PAGE_403_DELAY)
    raise AssertionError("unreachable")


def extract_info(url: str) -> dict:
    return ytdlp_extract(ytdlp_opts(), url, download=False, process=False)


def is_audio_only(info: dict) -> bool:
    if info.get("_type", "video") != "video":
        return False  # Playlists and redirects: let the user choose.
    formats = info.get("formats") or [info]
    return all(
        f.get("vcodec") == "none" or (f.get("vcodec") is None and f".{f.get('ext')}" in AUDIO_EXTS) for f in formats
    )


async def download_direct(
    url: str, dest_dir: str, progress: Progress, filename: str | None = None, headers: dict | None = None
) -> tuple[Path, dict]:
    async with http_client() as client:
        async with client.stream("GET", url, headers={"User-Agent": "Mozilla/5.0", **(headers or {})}) as resp:
            resp.raise_for_status()
            length = int(resp.headers.get("content-length") or 0)
            if length > MAX_SIZE:
                raise TooLarge(human_size(length))
            path = Path(dest_dir) / (safe_name(filename) or guess_filename(resp))
            size = 0
            start = time.monotonic()
            with path.open("wb") as f:
                async for chunk in resp.aiter_bytes(1024 * 256):
                    size += len(chunk)
                    if size > MAX_SIZE:
                        raise TooLarge(f"> {MAX_SIZE_MB} MB")
                    f.write(chunk)
                    speed = size / max(time.monotonic() - start, 1e-3)
                    eta = (length - size) / speed if length else None
                    progress.update(transfer_text(f"⬇️ Đang tải: {path.name}", size, length or None, speed, eta))
            return path, {}


def ytdlp_opts(**extra) -> dict:
    opts = {
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "ffmpeg_location": FFMPEG,
        **extra,
    }
    if PROXY:
        opts["proxy"] = PROXY
    if COOKIES_FILE.is_file():
        opts["cookiefile"] = str(COOKIES_FILE)
    return opts


def nuxt_data(html: str):
    """A NhacCuaTui page's Nuxt payload (a flat array whose ints are references) and a resolver for references."""
    match = re.search(r'<script[^>]*id="__NUXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if not match:
        raise yt_dlp.utils.DownloadError("NhacCuaTui page has no song data")
    data = json.loads(match.group(1))

    def value(ref):
        return data[ref] if isinstance(ref, int) and 0 <= ref < len(data) else ref

    return data, value


def nct_playlist(html: str) -> tuple[str | None, list[dict]]:
    """(playlist name, songs in order as {key, title, artist}) from a NhacCuaTui playlist page."""
    data, value = nuxt_data(html)
    playlist = next((d for d in data if isinstance(d, dict) and "listSong" in d), None)
    if playlist is None:
        raise yt_dlp.utils.DownloadError("NhacCuaTui page has no playlist data")
    songs = []
    for ref in value(playlist["listSong"]) or ():
        song = value(ref)
        if isinstance(song, dict) and value(song.get("key")):
            songs.append(
                {"key": value(song["key"]), "title": value(song.get("name")), "artist": value(song.get("artistName"))}
            )
    if not songs:
        raise yt_dlp.utils.DownloadError("NhacCuaTui playlist is empty")
    return value(playlist.get("name")), songs


def nct_song(html: str, key: str | None) -> dict:
    """The song record from a NhacCuaTui page's Nuxt payload, with references resolved one level deep."""
    data, value = nuxt_data(html)

    songs = [d for d in data if isinstance(d, dict) and "streamURL" in d and "key" in d]
    song = next((d for d in songs if value(d["key"]) == key), songs[0] if songs else None)
    if song is None:
        raise yt_dlp.utils.DownloadError("NhacCuaTui page has no song data")
    streams = [{k: value(v) for k, v in value(ref).items()} for ref in value(song["streamURL"]) or ()]
    return {
        "title": value(song.get("name")),
        "artist": value(song.get("artistName")),
        "duration": int(value(song.get("duration")) or 0) or None,
        "streams": [s for s in streams if s.get("stream") and s.get("status") == 1 and not s.get("onlyVIP")],
    }


async def download_nct(url: str, dest_dir: str, progress: Progress) -> tuple[Path, dict]:
    async with http_client() as client:
        resp = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
    song = nct_song(resp.text, nct_key(url))
    by_quality = {s.get("type"): s["stream"] for s in song["streams"]}
    quality = next((q for q in NCT_QUALITIES if q in by_quality), None)
    if quality is None:
        raise yt_dlp.utils.DownloadError("NhacCuaTui has no free MP3 for this song")
    name = " - ".join(x for x in (song["artist"], song["title"]) if x) or "nhaccuatui"
    path, _ = await download_direct(
        by_quality[quality], dest_dir, progress, f"{name}.mp3", headers={"Referer": "https://www.nhaccuatui.com/"}
    )
    info = {"title": song["title"], "artist": song["artist"], "duration": song["duration"], "abr": int(quality)}
    return path, info


def nhacvn_song(html: str) -> dict:
    """Title, artist and {label: mp3 url} from the jwplayer setup in a nhac.vn song page."""
    sources = re.search(r"sources:\s*(\[.*?\])\s*,\s*\n", html, re.S)
    if not sources:
        raise yt_dlp.utils.DownloadError("nhac.vn page has no player sources")

    def js_string(name: str) -> str | None:
        match = re.search(rf"\b{name}\s*:\s*'((?:[^'\\]|\\.)*)'", html[sources.end() :])
        if not match:
            return None
        return htmllib.unescape(re.sub(r"\\(.)", r"\1", match.group(1))).strip() or None

    files = {s.get("label"): s["file"] for s in json.loads(sources.group(1)) if isinstance(s, dict) and s.get("file")}
    return {"title": js_string("title"), "artist": js_string("description"), "sources": files}


def mp3_duration(path: Path) -> int | None:
    try:
        from mutagen.mp3 import MP3

        return int(MP3(path).info.length) or None
    except Exception:  # Only used for the player's duration display; Telegram can do without it.
        return None


async def download_nhacvn(url: str, dest_dir: str, progress: Progress) -> tuple[Path, dict]:
    async with http_client() as client:
        resp = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
    song = nhacvn_song(resp.text)
    quality = next((q for q in NHACVN_QUALITIES if q in song["sources"]), None)
    if quality is None:
        raise yt_dlp.utils.DownloadError("nhac.vn has no free MP3 for this song")
    name = " - ".join(x for x in (song["artist"], song["title"]) if x) or "nhacvn"
    path, _ = await download_direct(
        song["sources"][quality], dest_dir, progress, f"{name}.mp3", headers={"Referer": "https://nhac.vn/"}
    )
    info = {"title": song["title"], "artist": song["artist"], "duration": mp3_duration(path), "abr": int(quality[:-1])}
    return path, info


def estimate_size(info: dict) -> tuple[int, bool] | None:
    """(bytes, exact) for the format(s) yt-dlp selected, or None if unknown."""
    total, exact = 0, True
    for f in info.get("requested_formats") or [info]:
        if f.get("filesize"):
            total += f["filesize"]
        elif f.get("filesize_approx"):
            total += f["filesize_approx"]
            exact = False
        elif f.get("tbr") and info.get("duration"):
            total += f["tbr"] * 125 * info["duration"]
            exact = False
        else:
            return None
    return int(total), exact


def probe_sizes(ie_result: dict) -> dict[str, tuple[int, bool]]:
    """Expected file size per FORMATS key, from an extract_info() result without downloading."""
    if ie_result.get("_type", "video") != "video":
        return {}
    sizes = {}
    for fmt, (_, extra) in FORMATS.items():
        with yt_dlp.YoutubeDL(ytdlp_opts(**extra)) as ydl:
            info = ydl.process_ie_result(copy.deepcopy(ie_result), download=False)
        if size := estimate_size(info):
            sizes[fmt] = size
    return sizes


def format_button(fmt: str, size: tuple[int, bool] | None) -> str:
    label = FORMATS[fmt][0]
    if not size:
        return label
    n, exact = size
    return f"{label} · {'' if exact else '~'}{human_size(n)}{' ⚠️' if n > MAX_SIZE else ''}"


def download_ytdlp(url: str, dest_dir: str, fmt: str, progress: Progress) -> tuple[Path, dict]:
    def on_download(d: dict) -> None:
        if d["status"] == "downloading":
            name = d.get("info_dict", {}).get("title") or Path(d["filename"]).name
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            done = d.get("downloaded_bytes") or 0
            progress.update(transfer_text(f"⬇️ Đang tải: {name}", done, total, d.get("speed"), d.get("eta")))

    def on_postprocess(d: dict) -> None:
        if d["status"] == "started" and d.get("postprocessor") != "MoveFiles":
            progress.update("⚙️ Đang xử lý...", force=True)

    opts = ytdlp_opts(
        outtmpl=f"{dest_dir}/%(title).80B [%(id)s].%(ext)s",
        max_filesize=MAX_SIZE,
        progress_hooks=[on_download],
        postprocessor_hooks=[on_postprocess],
        **FORMATS[fmt][1],
    )
    info = ytdlp_extract(opts, url, download=True)
    files = [p for p in Path(dest_dir).iterdir() if p.is_file() and not p.name.endswith(".part")]
    if not files:
        # yt-dlp silently skips downloads above max_filesize.
        raise TooLarge(f"> {MAX_SIZE_MB} MB")
    path = max(files, key=lambda p: p.stat().st_size)
    if fmt == "audio" and path.suffix.lower() not in AUDIO_EXTS:
        progress.update("🎵 Đang tách âm thanh...", force=True)
        path = extract_audio(path, info.get("acodec"))
    return path, info


def extract_audio(src: Path, acodec: str | None) -> Path:
    """Copy the audio stream out of src (a video, or an audio-only .webm) without re-encoding."""
    codec = (acodec or "").split(".")[0].lower()
    dst = src.with_suffix(AUDIO_CONTAINERS.get(codec, ".mka"))
    subprocess.run(
        [FFMPEG, "-y", "-loglevel", "error", "-i", str(src), "-vn", "-map", "0:a:0", "-c:a", "copy", str(dst)],
        check=True,
        capture_output=True,
    )
    src.unlink()
    return dst


async def send_file(bot, chat_id, path: Path, caption: str, info: dict) -> int:
    ext = path.suffix.lower()
    duration = int(info["duration"]) if info.get("duration") else None
    # A local Bot API server reads the file straight from disk (the Path becomes a file:// URI),
    # which avoids loading multi-GB files into memory for a multipart upload.
    with nullcontext(path) if LOCAL_BOT_API_URL else path.open("rb") as f:
        if ext in VIDEO_EXTS:
            message = await bot.send_video(
                chat_id, video=f, filename=path.name, caption=caption, duration=duration, supports_streaming=True
            )
        elif ext in PLAYER_AUDIO_EXTS:
            message = await bot.send_audio(
                chat_id,
                audio=f,
                filename=path.name,
                caption=caption,
                duration=duration,
                title=info.get("title"),
                performer=info.get("artist") or info.get("uploader"),
            )
        else:
            message = await bot.send_document(chat_id, document=f, filename=path.name, caption=caption)
    return message.message_id


async def upload_parts(path: Path, on_progress) -> InputFile | InputFileBig:
    """Upload path to Telegram's servers in parallel parts, calling on_progress(done, total) as each is acknowledged."""
    size = path.stat().st_size
    parts = -(-size // UPLOAD_PART_SIZE)
    big = size > 10 * 1024 * 1024  # MTProto requires the "big file" methods above 10 MB.
    file_id = random.getrandbits(63)
    pending = iter(range(parts))  # Shared by the workers; each takes the next part number.
    done = 0

    async def worker() -> None:
        nonlocal done
        with path.open("rb") as f:
            for part in pending:
                f.seek(part * UPLOAD_PART_SIZE)
                data = f.read(UPLOAD_PART_SIZE)
                if big:
                    request = SaveBigFilePartRequest(file_id, part, parts, data)
                else:
                    request = SaveFilePartRequest(file_id, part, data)
                if not await mtproto(request):
                    raise RuntimeError(f"Telegram rejected part {part}")
                done += len(data)
                on_progress(done, size)

    try:
        async with asyncio.TaskGroup() as tg:
            for _ in range(UPLOAD_PARALLEL):
                tg.create_task(worker())
    except ExceptionGroup as eg:
        raise eg.exceptions[0] from None
    if big:
        return InputFileBig(file_id, parts, path.name)
    return InputFile(file_id, parts, path.name, md5_checksum="")


async def mtproto_peer(chat_id: int | str, message_id: int):
    try:
        return await mtproto.get_input_entity(chat_id)
    except ValueError:
        # A private chat's access hash is unknown to a fresh session; fetching one of the bot's own
        # messages there by id (allowed for bots) returns the user and caches it.
        await mtproto.get_messages(None, ids=message_id)
        return await mtproto.get_input_entity(chat_id)


async def send_file_mtproto(peer, path: Path, caption: str, info: dict, progress: Progress) -> int:
    started = time.monotonic()

    def on_progress(done: int, total: int) -> None:
        speed = done / max(time.monotonic() - started, 1e-3)
        progress.update(transfer_text(f"⬆️ Đang upload: {path.name}", done, total, speed, (total - done) / speed))

    file = await upload_parts(path, on_progress)
    progress.update("📨 Đang gửi...", force=True)
    ext = path.suffix.lower()
    duration = int(info["duration"]) if info.get("duration") else 0
    attributes = [DocumentAttributeFilename(path.name)]
    if ext in VIDEO_EXTS:
        attributes.append(
            DocumentAttributeVideo(duration, info.get("width") or 0, info.get("height") or 0, supports_streaming=True)
        )
    elif ext in PLAYER_AUDIO_EXTS:
        attributes.append(
            DocumentAttributeAudio(
                duration, title=info.get("title"), performer=info.get("artist") or info.get("uploader")
            )
        )
    message = await mtproto.send_file(
        peer,
        file,
        caption=caption,
        parse_mode=None,
        attributes=attributes,
        force_document=ext not in VIDEO_EXTS | PLAYER_AUDIO_EXTS,
        supports_streaming=ext in VIDEO_EXTS,
    )
    return message.id


async def keep_chat_action(bot, chat_id) -> None:
    while True:
        with suppress(TelegramError):
            await bot.send_chat_action(chat_id, ChatAction.UPLOAD_DOCUMENT)
        await asyncio.sleep(5)


async def report_upload(progress: Progress, name: str, size: int) -> None:
    """Tick the elapsed time while uploading through the Bot API, which reports no upload progress."""
    start = time.monotonic()
    while True:
        await progress.set(f"⬆️ Đang upload: {name} ({human_size(size)})\n⏱ {human_time(time.monotonic() - start)}")
        await asyncio.sleep(PROGRESS_INTERVAL + 2)


async def upload(
    context: ContextTypes.DEFAULT_TYPE, status, target, path: Path, caption: str, info: dict, progress: Progress
) -> int:
    """Send path to target; returns the new message's id."""
    peer = None
    if mtproto:
        try:
            peer = await mtproto_peer(target, status.message_id)
        except Exception as e:
            log.warning("MTProto can't reach chat %s, uploading via the Bot API: %r", target, e)
    tasks = [asyncio.create_task(keep_chat_action(context.bot, status.chat_id))]
    if not peer:
        tasks.append(asyncio.create_task(report_upload(progress, path.name, path.stat().st_size)))
    try:
        if peer:
            return await send_file_mtproto(peer, path, caption, info, progress)
        return await send_file(context.bot, target, path, caption, info)
    finally:
        for task in tasks:
            task.cancel()


def nct_key(url: str) -> str | None:
    match = NCT_RE.match(url)
    return match and (match.group(1) or match.group(2))


def song_record(target, url: str, fmt: str | None) -> str | None:
    """The uploaded.json key for a song from a music site, the same whatever form its link takes."""
    if fmt == "nct" and nct_key(url):
        return f"{target}|nct:{nct_key(url)}"
    if fmt == "nhacvn" and (match := NHACVN_RE.match(url)):
        return f"{target}|nhacvn:{match.group(1)}"
    return None


def load_uploaded() -> dict:
    try:
        return json.loads(UPLOADED_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_uploaded(record: str, message_id: int | None) -> None:
    uploaded = load_uploaded()
    if message_id is None:
        uploaded.pop(record, None)
    else:
        uploaded[record] = message_id
    tmp = UPLOADED_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(uploaded, ensure_ascii=False, indent=0), encoding="utf-8")
    tmp.replace(UPLOADED_PATH)  # Atomic, so a crash never leaves half a file.


async def already_uploaded(target, record: str) -> bool:
    """Whether the song in record was uploaded to target and (when MTProto can tell) is still there."""
    message_id = load_uploaded().get(record)
    if message_id is None:
        return False
    if mtproto:
        try:
            if await mtproto.get_messages(target, ids=message_id) is None:
                save_uploaded(record, None)  # Deleted from the channel: upload it again.
                return False
        except Exception as e:
            log.info("can't check message %s in %s, trusting the record: %r", message_id, target, e)
    return True


async def process_url(
    context: ContextTypes.DEFAULT_TYPE, status, url: str, fmt: str | None, prefix: str = "", markup=None
) -> bool:
    """Download url (directly, or with yt-dlp in format fmt) and upload it, reporting progress in status.

    Returns "ok", "skipped" (a music-site song target already has) or "failed"; failures are reported in status,
    not raised.
    """
    target = TARGET_CHAT_ID or status.chat_id
    progress = Progress(status, asyncio.get_running_loop(), prefix, markup)
    record = song_record(target, url, fmt)
    try:
        if record and await already_uploaded(target, record):
            await progress.set(f"⏭ Bài này đã có trong channel, bỏ qua: {url}")
            return "skipped"
        await context.bot.send_chat_action(status.chat_id, ChatAction.UPLOAD_DOCUMENT)
        await progress.set(f"⬇️ Đang tải: {url}")
        with tempfile.TemporaryDirectory(dir=DOWNLOAD_DIR) as tmp:
            # Under Docker the Bot API server runs as another user and must be able to read the file.
            os.chmod(tmp, 0o755)
            started = time.monotonic()
            if fmt == "nct":
                path, info = await download_nct(url, tmp, progress)
            elif fmt == "nhacvn":
                path, info = await download_nhacvn(url, tmp, progress)
            elif fmt:
                path, info = await asyncio.to_thread(download_ytdlp, url, tmp, fmt, progress)
            else:
                path, info = await download_direct(url, tmp, progress)
            download_time = time.monotonic() - started
            size = path.stat().st_size
            if size > MAX_SIZE:
                raise TooLarge(human_size(size))
            title = info.get("title")
            if fmt in ("nct", "nhacvn"):
                title = f"{' - '.join(x for x in (info.get('artist'), title) if x)} · MP3 {info['abr']} kbps"
            caption = f"{title[:900]}\n{url}" if title else url
            started = time.monotonic()
            message_id = await upload(context, status, target, path, caption, info, progress)
            if record:
                save_uploaded(record, message_id)
            upload_time = time.monotonic() - started
        await progress.set(
            f"✅ Xong: {path.name} ({human_size(size)})\n"
            f"⬇️ tải {human_time(download_time)} · ⬆️ upload {human_time(upload_time)}"
        )
        return "ok"
    except TooLarge as e:
        hint = " Thử chọn 📱 MP4 360p hoặc 🎵 Audio gốc." if fmt == "mp4" else ""
        await progress.set(f"❌ File quá lớn ({e}), giới hạn {MAX_SIZE_MB} MB: {url}{hint}")
    except subprocess.CalledProcessError as e:
        log.warning("audio extraction failed for %s: %s", url, e.stderr)
        await progress.set(f"❌ Không tách được âm thanh (video không có âm thanh?): {url}")
    except (httpx.HTTPError, yt_dlp.utils.DownloadError) as e:
        log.warning("download failed for %s: %r", url, e)
        await progress.set(login_text(url) if needs_login(e) else f"❌ Lỗi tải {url}: {type(e).__name__}: {e}")
    except Exception as e:
        log.exception("upload failed for %s", url)
        await progress.set(f"❌ Lỗi upload {url}: {e}")
    return "failed"


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Gửi cho mình 1 link (hoặc nhiều link) hoặc 1 file, mình sẽ upload lên Telegram.\n"
        "Link video (TikTok, YouTube...) sẽ cho chọn MP4 / Audio; link chỉ có âm thanh thì tải luôn.\n"
        "Âm thanh luôn giữ nguyên chất lượng gốc (không mã hoá lại).\n"
        "Có thể dùng: /up <link>\n"
        f"Giới hạn mỗi file: {MAX_SIZE_MB} MB.\n"
        f"User ID của bạn: {update.effective_user.id}"
    )


# Sites whose yt-dlp extractor is broken, with the reason shown instead of the generic "no media" error.
BROKEN_SITES = {
    "zingmp3.vn": "Zing MP3 đang chặn mọi công cụ tải (lỗi đã được báo cho yt-dlp nhưng chưa sửa), "
    "kể cả bài miễn phí. Hãy tìm bài trên NhacCuaTui hoặc YouTube rồi gửi link đó.",
}


def needs_login(error: Exception) -> bool:
    text = str(error)
    return "--cookies" in text or "Log in for access" in text or "Sign in to confirm" in text


def login_text(url: str) -> str:
    if COOKIES_FILE.is_file():
        return (
            f"🔒 Video này cần tài khoản đăng nhập (giới hạn độ tuổi / riêng tư): {url}\n"
            f"Bot đã dùng {COOKIES_FILE.name} nhưng vẫn bị từ chối: cookie có thể đã hết hạn, hãy xuất lại."
        )
    return (
        f"🔒 Video này cần tài khoản đăng nhập (giới hạn độ tuổi / riêng tư): {url}\n"
        f"Đăng nhập trang đó trên trình duyệt, xuất cookie ra file cookies.txt (tiện ích \"Get cookies.txt LOCALLY\") "
        f"rồi chép vào {COOKIES_FILE.parent}. Không cần khởi động lại bot."
    )


def no_media_text(url: str, error: Exception) -> str:
    if needs_login(error):
        return login_text(url)
    host = (httpx.URL(url).host or "").removeprefix("www.").removeprefix("m.")
    if host in BROKEN_SITES:
        return f"❌ Không tải được: {url}\n{BROKEN_SITES[host]}"
    # yt-dlp messages look like "ERROR: [site] id: reason; please report this issue ..."
    reason = re.sub(r"^ERROR: (\[[^]]+\] )?([\w-]+: )?", "", str(error)).split("; please report")[0].strip()
    return f"❌ Không tìm thấy video/audio trong trang: {url}" + (f"\n({reason[:300]})" if reason else "")


async def handle_links(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return
    urls = extract_urls(update.message)
    if not urls:
        await update.message.reply_text("Không tìm thấy link nào. Dùng: /up <link>")
        return

    for url in urls:
        status = await update.message.reply_text(f"🔎 Đang kiểm tra: {url}", disable_web_page_preview=True)
        if NCT_RE.match(url):
            await process_url(context, status, url, "nct")
            continue
        if NHACVN_RE.match(url):
            await process_url(context, status, url, "nhacvn")
            continue
        if NCT_PLAYLIST_RE.match(url):
            await show_playlist(context, status, url)
            continue
        try:
            kind, info = await classify_link(url)
        except httpx.HTTPError as e:
            log.warning("probe failed for %s: %r", url, e)
            await status.edit_text(
                f"❌ Máy chạy bot không kết nối được tới {url} ({type(e).__name__}).\n"
                "Mạng của máy chạy bot có thể đang chặn trang này: đổi mạng cho máy đó hoặc đặt PROXY trong .env."
            )
            continue
        except yt_dlp.utils.DownloadError as e:
            log.info("no media found at %s: %r", url, e)
            await status.edit_text(no_media_text(url, e), disable_web_page_preview=True)
            continue
        if kind == "file":
            await process_url(context, status, url, None)
            continue
        if kind == "audio":
            await process_url(context, status, url, "audio")
            continue
        key = remember(context, "pending", url)
        await status.edit_text(
            f"Chọn định dạng cho:\n{url}\n⏳ Đang tính dung lượng...",
            reply_markup=format_keyboard(key, {}),
            disable_web_page_preview=True,
        )
        # Probing can take 10+ seconds, so the menu is usable right away and the sizes are filled in later.
        context.application.create_task(add_sizes(context, status, url, info, key))


def format_keyboard(key: str, sizes: dict) -> InlineKeyboardMarkup:
    # One button per row so the size fits next to the label.
    rows = [[InlineKeyboardButton(format_button(fmt, sizes.get(fmt)), callback_data=f"{fmt}:{key}")] for fmt in FORMATS]
    return InlineKeyboardMarkup([*rows, [InlineKeyboardButton("✖️ Huỷ", callback_data=f"cancel:{key}")]])


async def add_sizes(context: ContextTypes.DEFAULT_TYPE, status, url: str, info: dict | None, key: str) -> None:
    try:
        sizes = await asyncio.to_thread(probe_sizes, info or await asyncio.to_thread(extract_info, url))
    except Exception as e:
        log.info("size probe failed for %s: %r", url, e)
        sizes = {}
    if key not in context.bot_data.get("pending", {}):
        return  # A format was already chosen (or cancelled); don't put the menu back.
    note = "" if sizes else "\n(không xác định được dung lượng)"
    with suppress(TelegramError):
        await status.edit_text(
            f"Chọn định dạng cho:\n{url}{note}",
            reply_markup=format_keyboard(key, sizes),
            disable_web_page_preview=True,
        )


async def handle_format_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not is_allowed(update):
        await query.answer("Bạn không có quyền dùng bot này.")
        return
    await query.answer()
    fmt, _, key = (query.data or "").partition(":")
    if fmt != "cancel" and fmt not in FORMATS:
        return  # Not one of our buttons (e.g. a menu from an older version of the bot).
    url = context.bot_data.get("pending", {}).pop(key, None)
    if fmt == "cancel":
        await query.edit_message_text("✖️ Đã huỷ.")
    elif url is None:
        await query.edit_message_text("⌛ Link đã hết hạn (bot vừa khởi động lại?), hãy gửi lại link.")
    else:
        await process_url(context, query.message, url, fmt)


# --- NhacCuaTui playlists: pick songs (all ticked at first), then download them one by one ----------------------


def remember(context: ContextTypes.DEFAULT_TYPE, name: str, value) -> str:
    """Keep value in bot_data[name] under a short key that fits in callback data (limited to 64 bytes)."""
    key = uuid.uuid4().hex[:12]
    store = context.bot_data.setdefault(name, {})
    store[key] = value
    while len(store) > MAX_PENDING:
        store.pop(next(iter(store)))  # Forget the oldest unanswered menu.
    return key


def song_label(song: dict) -> str:
    return " - ".join(x for x in (song.get("artist"), song.get("title")) if x) or song["key"]


def playlist_text(pl: dict) -> str:
    return (
        f"📃 Playlist: {pl['title'] or pl['url']} ({len(pl['songs'])} bài)\n"
        f"Đã chọn {len(pl['selected'])}/{len(pl['songs'])} bài. Bấm vào bài để chọn / bỏ chọn."
    )


def playlist_keyboard(key: str, pl: dict) -> InlineKeyboardMarkup:
    pages = max(1, -(-len(pl["songs"]) // PLAYLIST_PAGE_SIZE))
    page = min(pl["page"], pages - 1)
    first = page * PLAYLIST_PAGE_SIZE
    rows = []
    for i, song in enumerate(pl["songs"][first : first + PLAYLIST_PAGE_SIZE], start=first):
        mark = "✅" if i in pl["selected"] else "⬜"
        rows.append([InlineKeyboardButton(f"{mark} {i + 1}. {song_label(song)}"[:60], callback_data=f"pl:{key}:t:{i}")])
    if pages > 1:
        rows.append([
            InlineKeyboardButton("◀️", callback_data=f"pl:{key}:p:{(page - 1) % pages}"),
            InlineKeyboardButton(f"Trang {page + 1}/{pages}", callback_data=f"pl:{key}:noop"),
            InlineKeyboardButton("▶️", callback_data=f"pl:{key}:p:{(page + 1) % pages}"),
        ])  # fmt: skip
    rows.append([
        InlineKeyboardButton("☑️ Chọn hết", callback_data=f"pl:{key}:all"),
        InlineKeyboardButton("⬜ Bỏ hết", callback_data=f"pl:{key}:none"),
    ])  # fmt: skip
    rows.append([
        InlineKeyboardButton(f"⬇️ Tải {len(pl['selected'])} bài", callback_data=f"pl:{key}:go"),
        InlineKeyboardButton("✖️ Huỷ", callback_data=f"pl:{key}:x"),
    ])  # fmt: skip
    return InlineKeyboardMarkup(rows)


async def show_playlist(context: ContextTypes.DEFAULT_TYPE, status, url: str) -> None:
    try:
        async with http_client() as client:
            resp = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
            resp.raise_for_status()
        title, songs = nct_playlist(resp.text)
    except (httpx.HTTPError, yt_dlp.utils.DownloadError) as e:
        log.warning("playlist failed for %s: %r", url, e)
        await status.edit_text(f"❌ Không đọc được playlist {url}: {type(e).__name__}: {e}")
        return
    pl = {"url": url, "title": title, "songs": songs, "selected": set(range(len(songs))), "page": 0}
    key = remember(context, "playlists", pl)
    await status.edit_text(playlist_text(pl), reply_markup=playlist_keyboard(key, pl), disable_web_page_preview=True)


async def handle_playlist_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not is_allowed(update):
        await query.answer("Bạn không có quyền dùng bot này.")
        return
    _, key, action, *arg = [*(query.data or "").split(":"), "", ""][:4]
    playlists = context.bot_data.get("playlists", {})
    pl = playlists.get(key)
    if pl is None:
        await query.answer()
        await query.edit_message_text("⌛ Danh sách đã hết hạn (bot vừa khởi động lại?), hãy gửi lại link.")
        return
    if action == "stop":
        pl["stop"] = True
        await query.answer("Sẽ dừng sau bài đang tải.")
        return
    if pl.get("running"):
        await query.answer("Playlist đang được tải.")
        return
    if action == "go" and not pl["selected"]:
        await query.answer("Chưa chọn bài nào.")
        return
    await query.answer()
    index = int(arg[0]) if arg and arg[0].isdigit() else None
    if action == "t" and index is not None and index < len(pl["songs"]):
        pl["selected"] ^= {index}
    elif action == "p" and index is not None:
        pl["page"] = index
    elif action == "all":
        pl["selected"] = set(range(len(pl["songs"])))
    elif action == "none":
        pl["selected"] = set()
    elif action == "x":
        playlists.pop(key, None)
        await query.edit_message_text("✖️ Đã huỷ.")
        return
    elif action == "go":
        pl["running"] = True  # A double tap on "Tải" must not start a second download.
        await download_playlist(context, query.message, key, pl)
        return
    else:
        return  # "noop" (the page number) or an unknown button
    with suppress(TelegramError):  # "message is not modified" when nothing changed
        await query.edit_message_text(
            playlist_text(pl), reply_markup=playlist_keyboard(key, pl), disable_web_page_preview=True
        )


async def download_playlist(context: ContextTypes.DEFAULT_TYPE, status, key: str, pl: dict) -> None:
    """Download and upload the selected songs one at a time, in playlist order, in one status message."""
    stop = InlineKeyboardMarkup([[InlineKeyboardButton("⏹ Dừng", callback_data=f"pl:{key}:stop")]])
    songs = [pl["songs"][i] for i in sorted(pl["selected"])]
    failed = []
    done = skipped = 0
    for n, song in enumerate(songs, start=1):
        if pl.get("stop"):
            break
        prefix = f"📃 {pl['title'] or 'Playlist'} · bài {n}/{len(songs)}: {song_label(song)}\n"
        url = f"https://www.nhaccuatui.com/song/{song['key']}"
        result = await process_url(context, status, url, "nct", prefix=prefix, markup=stop)
        if result == "ok":
            done += 1
        elif result == "skipped":
            skipped += 1
        else:
            failed.append(song_label(song))
    context.bot_data.get("playlists", {}).pop(key, None)
    lines = [f"✅ Playlist {pl['title'] or pl['url']}: tải xong {done}/{len(songs)} bài."]
    if skipped:
        lines.append(f"⏭ Bỏ qua {skipped} bài đã có trong channel.")
    if pl.get("stop"):
        lines.append("⏹ Đã dừng theo yêu cầu.")
    if failed:
        lines.append("❌ Lỗi: " + "; ".join(failed)[:3000])
    with suppress(TelegramError):
        await status.edit_text("\n".join(lines), disable_web_page_preview=True)


async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return
    if not TARGET_CHAT_ID:
        await update.message.reply_text(
            "File đã nằm trên Telegram rồi. Đặt TARGET_CHAT_ID để bot tự đẩy file sang channel/group đích."
        )
        return
    try:
        # copy_message re-uses Telegram's stored file, so there is no size limit and no download.
        await context.bot.copy_message(TARGET_CHAT_ID, update.effective_chat.id, update.message.message_id)
        await update.message.reply_text("✅ Đã upload lên đích.")
    except Exception as e:
        log.exception("copy failed")
        await update.message.reply_text(f"❌ Lỗi: {e}")


async def start_mtproto(app: Application) -> None:
    global mtproto
    if not MTPROTO_UPLOAD:
        return
    client = TelegramClient(str(HOME / "mtproto"), API_ID, API_HASH, receive_updates=False)
    try:
        await client.start(bot_token=BOT_TOKEN)
    except Exception:
        log.exception("MTProto login failed, uploading via the Bot API")
        return
    mtproto = client
    log.info("Uploading over MTProto")


async def stop_mtproto(app: Application) -> None:
    if mtproto:
        await mtproto.disconnect()


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    if isinstance(context.error, Conflict):
        if time.monotonic() - STARTED_AT < TAKEOVER_GRACE:
            log.warning("Another machine is polling this bot; taking it over.")
            return
        log.warning("Another machine started this bot; stopping here so it keeps the bot.")
        context.application.stop_running()
        return
    log.error("Unhandled error", exc_info=context.error)


def build_app() -> Application:
    builder = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .connect_timeout(60)
        .read_timeout(600)
        .write_timeout(600)
        .media_write_timeout(600)
        .concurrent_updates(True)
        .post_init(start_mtproto)
        .post_shutdown(stop_mtproto)
    )
    if LOCAL_BOT_API_URL:
        builder = (
            builder.base_url(f"{LOCAL_BOT_API_URL}/bot")
            .base_file_url(f"{LOCAL_BOT_API_URL}/file/bot")
            .local_mode(True)
        )
    app = builder.build()

    app.add_handler(CommandHandler(["start", "help"], start))
    app.add_handler(CommandHandler(["up", "link"], handle_links))
    app.add_handler(CallbackQueryHandler(handle_playlist_choice, pattern=r"^pl:"))
    app.add_handler(CallbackQueryHandler(handle_format_choice))
    file_filter = (
        filters.Document.ALL | filters.PHOTO | filters.VIDEO | filters.AUDIO | filters.VOICE | filters.ANIMATION
    )
    app.add_handler(MessageHandler(file_filter, handle_file))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_links))
    app.add_error_handler(on_error)
    return app


def main() -> None:
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    if not ALLOWED_USER_IDS:
        log.warning("ALLOWED_USER_IDS is empty: anyone who finds this bot can use it.")
    log.info("Bot started. Target: %s, max size: %d MB", TARGET_CHAT_ID or "same chat", MAX_SIZE_MB)
    build_app().run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
