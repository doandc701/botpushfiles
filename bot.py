"""Telegram bot: paste a link or send a file and it gets uploaded to Telegram."""

import asyncio
import copy
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
DOWNLOAD_DIR = HOME / "downloads"
# System ffmpeg if installed, otherwise the binary bundled with imageio-ffmpeg.
FFMPEG = shutil.which("ffmpeg") or imageio_ffmpeg.get_ffmpeg_exe()

URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
# NhacCuaTui song pages: /song/<key> and the older /bai-hat/<slug>.<key>.html. yt-dlp has no extractor for them.
NCT_RE = re.compile(r"^https?://(?:www\.|m\.)?nhaccuatui\.com/(?:song/([\w-]+)|bai-hat/[^/?#]*\.([\w-]+)\.html)", re.I)
# NhacCuaTui qualities from best to worst that Telegram plays in its music player; FLAC would arrive as a document.
NCT_QUALITIES = ("320", "128")
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
    "audio": ("🎵 Audio gốc", {"format": "ba/b"}),
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

    def __init__(self, status, loop: asyncio.AbstractEventLoop):
        self.status = status
        self.loop = loop
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
            if seq != self.seq or text == self.shown:
                return
            try:
                await self.status.edit_text(text, disable_web_page_preview=True)
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
    urls = [u if re.match(r"https?://", u, re.I) else f"https://{u}" for u in found]
    return list(dict.fromkeys(urls))  # De-duplicate, keeping order.


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


def extract_info(url: str) -> dict:
    with yt_dlp.YoutubeDL(ytdlp_opts()) as ydl:
        return ydl.extract_info(url, download=False, process=False)


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
    return opts


def nct_song(html: str, key: str | None) -> dict:
    """The song record from a NhacCuaTui page's Nuxt payload, with references resolved one level deep."""
    match = re.search(r'<script[^>]*id="__NUXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if not match:
        raise yt_dlp.utils.DownloadError("NhacCuaTui page has no song data")
    data = json.loads(match.group(1))

    def value(ref):
        return data[ref] if isinstance(ref, int) and 0 <= ref < len(data) else ref

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
    match = NCT_RE.match(url)
    async with http_client() as client:
        resp = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
    song = nct_song(resp.text, match and (match.group(1) or match.group(2)))
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
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
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


async def send_file(bot, chat_id, path: Path, caption: str, info: dict) -> None:
    ext = path.suffix.lower()
    duration = int(info["duration"]) if info.get("duration") else None
    # A local Bot API server reads the file straight from disk (the Path becomes a file:// URI),
    # which avoids loading multi-GB files into memory for a multipart upload.
    with nullcontext(path) if LOCAL_BOT_API_URL else path.open("rb") as f:
        if ext in VIDEO_EXTS:
            await bot.send_video(
                chat_id, video=f, filename=path.name, caption=caption, duration=duration, supports_streaming=True
            )
        elif ext in PLAYER_AUDIO_EXTS:
            await bot.send_audio(
                chat_id,
                audio=f,
                filename=path.name,
                caption=caption,
                duration=duration,
                title=info.get("title"),
                performer=info.get("artist") or info.get("uploader"),
            )
        else:
            await bot.send_document(chat_id, document=f, filename=path.name, caption=caption)


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


async def send_file_mtproto(peer, path: Path, caption: str, info: dict, progress: Progress) -> None:
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
    await mtproto.send_file(
        peer,
        file,
        caption=caption,
        parse_mode=None,
        attributes=attributes,
        force_document=ext not in VIDEO_EXTS | PLAYER_AUDIO_EXTS,
        supports_streaming=ext in VIDEO_EXTS,
    )


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
) -> None:
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
            await send_file_mtproto(peer, path, caption, info, progress)
        else:
            await send_file(context.bot, target, path, caption, info)
    finally:
        for task in tasks:
            task.cancel()


async def process_url(context: ContextTypes.DEFAULT_TYPE, status, url: str, fmt: str | None) -> None:
    """Download url (directly, or with yt-dlp in format fmt) and upload it, reporting progress in status."""
    target = TARGET_CHAT_ID or status.chat_id
    progress = Progress(status, asyncio.get_running_loop())
    try:
        await context.bot.send_chat_action(status.chat_id, ChatAction.UPLOAD_DOCUMENT)
        await progress.set(f"⬇️ Đang tải: {url}")
        with tempfile.TemporaryDirectory(dir=DOWNLOAD_DIR) as tmp:
            # Under Docker the Bot API server runs as another user and must be able to read the file.
            os.chmod(tmp, 0o755)
            started = time.monotonic()
            if fmt == "nct":
                path, info = await download_nct(url, tmp, progress)
            elif fmt:
                path, info = await asyncio.to_thread(download_ytdlp, url, tmp, fmt, progress)
            else:
                path, info = await download_direct(url, tmp, progress)
            download_time = time.monotonic() - started
            size = path.stat().st_size
            if size > MAX_SIZE:
                raise TooLarge(human_size(size))
            title = info.get("title")
            if fmt == "nct":
                title = f"{' - '.join(x for x in (info.get('artist'), title) if x)} · MP3 {info['abr']} kbps"
            caption = f"{title[:900]}\n{url}" if title else url
            started = time.monotonic()
            await upload(context, status, target, path, caption, info, progress)
            upload_time = time.monotonic() - started
        await progress.set(
            f"✅ Xong: {path.name} ({human_size(size)})\n"
            f"⬇️ tải {human_time(download_time)} · ⬆️ upload {human_time(upload_time)}"
        )
    except TooLarge as e:
        hint = " Thử chọn 📱 MP4 360p hoặc 🎵 Audio gốc." if fmt == "mp4" else ""
        await progress.set(f"❌ File quá lớn ({e}), giới hạn {MAX_SIZE_MB} MB: {url}{hint}")
    except subprocess.CalledProcessError as e:
        log.warning("audio extraction failed for %s: %s", url, e.stderr)
        await progress.set(f"❌ Không tách được âm thanh (video không có âm thanh?): {url}")
    except (httpx.HTTPError, yt_dlp.utils.DownloadError) as e:
        log.warning("download failed for %s: %r", url, e)
        await progress.set(f"❌ Lỗi tải {url}: {type(e).__name__}: {e}")
    except Exception as e:
        log.exception("upload failed for %s", url)
        await progress.set(f"❌ Lỗi upload {url}: {e}")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Gửi cho mình 1 link (hoặc nhiều link) hoặc 1 file, mình sẽ upload lên Telegram.\n"
        "Link video (TikTok, YouTube...) sẽ cho chọn MP4 / Audio; link chỉ có âm thanh thì tải luôn.\n"
        "Âm thanh luôn giữ nguyên chất lượng gốc (không mã hoá lại).\n"
        "Có thể dùng: /up <link>\n"
        f"Giới hạn mỗi file: {MAX_SIZE_MB} MB.\n"
        f"User ID của bạn: {update.effective_user.id}"
    )


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
            await status.edit_text(f"❌ Không tìm thấy video/audio trong trang: {url}", disable_web_page_preview=True)
            continue
        if kind == "file":
            await process_url(context, status, url, None)
            continue
        if kind == "audio":
            await process_url(context, status, url, "audio")
            continue
        # Callback data is limited to 64 bytes, so keep the URL server-side under a short key.
        key = uuid.uuid4().hex[:12]
        pending = context.bot_data.setdefault("pending", {})
        pending[key] = url
        while len(pending) > MAX_PENDING:
            pending.pop(next(iter(pending)))  # Forget the oldest unanswered menu.
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
