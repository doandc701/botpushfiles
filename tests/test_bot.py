import asyncio
import subprocess
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from telegram.error import Conflict, NetworkError

import bot

# --- Formatting helpers -------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("n", "expected"),
    [(0, "0.0 B"), (1023, "1023.0 B"), (1024, "1.0 KB"), (5 * 1024**2, "5.0 MB"), (3 * 1024**4, "3.0 TB")],
)
def test_human_size(n, expected):
    assert bot.human_size(n) == expected


@pytest.mark.parametrize(("s", "expected"), [(0, "0:00"), (65, "1:05"), (3661, "1:01:01"), (59.9, "0:59")])
def test_human_time(s, expected):
    assert bot.human_time(s) == expected


def test_transfer_text_with_total():
    text = bot.transfer_text("⬇️ x", 512, 1024, 256, 2)
    assert text.splitlines() == ["⬇️ x", "██████░░░░░░ 50%", "512.0 B / 1.0 KB · 256.0 B/s · còn 0:02"]


def test_transfer_text_unknown_total_and_overshoot():
    assert bot.transfer_text("t", 10, None, None, None) == "t\n10.0 B"
    # A size estimate that turns out too small must not draw past 100 %.
    assert "100%" in bot.transfer_text("t", 2000, 1000, None, None)


def test_format_button_marks_estimates_and_oversize():
    assert bot.format_button("mp4", None) == "🎬 MP4"
    assert bot.format_button("mp4", (1024, True)) == "🎬 MP4 · 1.0 KB"
    assert bot.format_button("mp4low", (1024, False)) == "📱 MP4 360p · ~1.0 KB"
    assert bot.format_button("audio", (bot.MAX_SIZE + 1, True)).endswith("⚠️")


def test_format_keyboard_callback_data_fits_telegram_limit():
    keyboard = bot.format_keyboard("a" * 12, {})
    data = [b.callback_data for row in keyboard.inline_keyboard for b in row]
    assert data == ["mp4:aaaaaaaaaaaa", "mp4low:aaaaaaaaaaaa", "audio:aaaaaaaaaaaa", "cancel:aaaaaaaaaaaa"]
    assert all(len(d.encode()) <= 64 for d in data)


# --- Access control -----------------------------------------------------------------------------------------------


def update_from(user_id):
    return SimpleNamespace(effective_user=SimpleNamespace(id=user_id) if user_id else None)


def test_is_allowed():
    assert bot.is_allowed(update_from(111))
    assert not bot.is_allowed(update_from(999))
    assert not bot.is_allowed(update_from(None))


def test_is_allowed_when_list_is_empty(monkeypatch):
    monkeypatch.setattr(bot, "ALLOWED_USER_IDS", set())
    assert bot.is_allowed(update_from(999))


# --- Link classification and sizes --------------------------------------------------------------------------------


def test_is_audio_only():
    assert bot.is_audio_only({"formats": [{"vcodec": "none"}, {"vcodec": None, "ext": "mp3"}]})
    assert not bot.is_audio_only({"formats": [{"vcodec": "none"}, {"vcodec": "avc1"}]})
    assert not bot.is_audio_only({"_type": "playlist"})
    assert bot.is_audio_only({"vcodec": "none"})  # No format list: the info itself is the only format.


def test_estimate_size():
    assert bot.estimate_size({"filesize": 100}) == (100, True)
    merged = {"requested_formats": [{"filesize": 100}, {"filesize_approx": 50}]}
    assert bot.estimate_size(merged) == (150, False)
    assert bot.estimate_size({"tbr": 8, "duration": 10}) == (10_000, False)  # 8 kbit/s * 10 s
    assert bot.estimate_size({"requested_formats": [{"filesize": 100}, {}]}) is None


@pytest.mark.parametrize(
    ("headers", "url", "expected"),
    [
        ({"content-disposition": 'attachment; filename="report.pdf"'}, "https://x/dl", "report.pdf"),
        ({"content-disposition": 'attachment; filename="../../etc/passwd"'}, "https://x/dl", "passwd"),
        ({"content-disposition": 'attachment; filename=".."'}, "https://x/a.zip", "a.zip"),
        ({}, "https://x/path/My%20File.mp4?x=1", "My File.mp4"),
        ({}, "https://x/", "file"),
        ({}, "https://x/..", "file"),
    ],
)
def test_guess_filename_stays_inside_download_dir(headers, url, expected):
    response = httpx.Response(200, headers=headers, request=httpx.Request("GET", url))
    assert bot.guess_filename(response) == expected


def mock_client(monkeypatch, handler):
    monkeypatch.setattr(bot, "http_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))


@pytest.mark.parametrize(
    ("ctype", "url", "kind"),
    [
        ("video/mp4", "https://x/v", "video"),
        ("application/octet-stream", "https://x/clip.MKV", "video"),
        ("audio/mpeg", "https://x/song.mp3", "file"),
        ("application/zip", "https://x/a.zip", "file"),
    ],
)
async def test_classify_direct_links(monkeypatch, ctype, url, kind):
    mock_client(monkeypatch, lambda request: httpx.Response(200, headers={"content-type": ctype}))
    assert await bot.classify_link(url) == (kind, None)


async def test_classify_html_page_uses_ytdlp(monkeypatch):
    mock_client(monkeypatch, lambda request: httpx.Response(200, headers={"content-type": "text/html"}))
    monkeypatch.setattr(bot, "extract_info", lambda url: {"formats": [{"vcodec": "none"}]})
    assert await bot.classify_link("https://x/page") == ("audio", {"formats": [{"vcodec": "none"}]})


async def test_classify_falls_back_to_ytdlp_when_http_fails(monkeypatch):
    def refuse(request):
        raise httpx.ConnectError("blocked", request=request)

    mock_client(monkeypatch, refuse)
    monkeypatch.setattr(bot, "extract_info", lambda url: {"formats": [{"vcodec": "avc1"}]})
    assert (await bot.classify_link("https://x/v"))[0] == "video"


# --- Downloads ----------------------------------------------------------------------------------------------------


class FakeStatus:
    def __init__(self):
        self.texts = []
        self.chat_id = 1
        self.message_id = 2

    async def edit_text(self, text, **kwargs):
        self.texts.append(text)


async def test_download_direct(monkeypatch, tmp_path):
    body = b"x" * 3000
    headers = {"content-disposition": "attachment; filename=a.bin"}
    mock_client(monkeypatch, lambda request: httpx.Response(200, content=body, headers=headers))
    progress = bot.Progress(FakeStatus(), asyncio.get_running_loop())
    path, info = await bot.download_direct("https://x/dl", str(tmp_path), progress)
    assert path == tmp_path / "a.bin"
    assert path.read_bytes() == body
    assert info == {}


async def test_download_direct_rejects_oversized_by_header(monkeypatch, tmp_path):
    big = str(bot.MAX_SIZE + 1)
    mock_client(monkeypatch, lambda request: httpx.Response(200, headers={"content-length": big}, content=b""))
    progress = bot.Progress(FakeStatus(), asyncio.get_running_loop())
    with pytest.raises(bot.TooLarge):
        await bot.download_direct("https://x/big", str(tmp_path), progress)


async def test_download_direct_rejects_oversized_stream(monkeypatch, tmp_path):
    async def stream():
        for _ in range(5):
            yield b"x" * (bot.MAX_SIZE // 4)

    mock_client(monkeypatch, lambda request: httpx.Response(200, content=stream()))
    progress = bot.Progress(FakeStatus(), asyncio.get_running_loop())
    with pytest.raises(bot.TooLarge):
        await bot.download_direct("https://x/big", str(tmp_path), progress)


def test_extract_audio_copies_stream_without_reencoding(tmp_path):
    video = tmp_path / "clip.mp4"
    subprocess.run(
        [bot.FFMPEG, "-loglevel", "error", "-f", "lavfi", "-i", "color=c=black:s=64x64:d=1",
         "-f", "lavfi", "-i", "sine=d=1", "-c:v", "libx264", "-c:a", "aac", "-shortest", str(video)],
        check=True,
    )  # fmt: skip
    audio = bot.extract_audio(video, "mp4a.40.2")
    assert audio == tmp_path / "clip.m4a"
    assert audio.stat().st_size > 0
    assert not video.exists()


def test_extract_audio_fails_on_silent_video(tmp_path):
    video = tmp_path / "silent.mp4"
    subprocess.run(
        [bot.FFMPEG, "-loglevel", "error", "-f", "lavfi", "-i", "color=c=black:s=64x64:d=1", str(video)], check=True
    )
    with pytest.raises(subprocess.CalledProcessError):
        bot.extract_audio(video, None)


# --- Progress -----------------------------------------------------------------------------------------------------


async def test_progress_throttles_and_skips_duplicates():
    status = FakeStatus()
    progress = bot.Progress(status, asyncio.get_running_loop())
    progress.update("a")
    progress.update("b")  # Within PROGRESS_INTERVAL: dropped.
    progress.update("c", force=True)
    await asyncio.sleep(0.05)
    await progress.set("c")  # Same as what is shown: no edit.
    assert status.texts == ["c"]  # "a" was superseded by the forced "c" before it was sent.


# --- Telegram handlers --------------------------------------------------------------------------------------------


def callback_update(data, user_id=111):
    query = SimpleNamespace(data=data, answer=AsyncMock(), edit_message_text=AsyncMock(), message=FakeStatus())
    return SimpleNamespace(effective_user=SimpleNamespace(id=user_id), callback_query=query)


def handler_context(pending=None):
    return SimpleNamespace(bot_data={"pending": dict(pending or {})}, bot=MagicMock())


async def test_format_choice_cancel():
    update, context = callback_update("cancel:k1"), handler_context({"k1": "https://x"})
    await bot.handle_format_choice(update, context)
    update.callback_query.edit_message_text.assert_awaited_once_with("✖️ Đã huỷ.")
    assert context.bot_data["pending"] == {}


async def test_format_choice_expired():
    update = callback_update("mp4:gone")
    await bot.handle_format_choice(update, handler_context())
    assert "hết hạn" in update.callback_query.edit_message_text.await_args.args[0]


async def test_format_choice_runs_download(monkeypatch):
    process_url = AsyncMock()
    monkeypatch.setattr(bot, "process_url", process_url)
    update, context = callback_update("audio:k1"), handler_context({"k1": "https://x"})
    await bot.handle_format_choice(update, context)
    process_url.assert_awaited_once_with(context, update.callback_query.message, "https://x", "audio")


@pytest.mark.parametrize("data", ["bogus:k1", "nocolon", None])
async def test_format_choice_ignores_unknown_buttons(data):
    update, context = callback_update(data), handler_context({"k1": "https://x"})
    await bot.handle_format_choice(update, context)
    update.callback_query.edit_message_text.assert_not_awaited()
    assert context.bot_data["pending"] == {"k1": "https://x"}


async def test_format_choice_rejects_strangers():
    update, context = callback_update("mp4:k1", user_id=999), handler_context({"k1": "https://x"})
    await bot.handle_format_choice(update, context)
    update.callback_query.answer.assert_awaited_once_with("Bạn không có quyền dùng bot này.")
    assert context.bot_data["pending"] == {"k1": "https://x"}


async def test_links_without_url():
    message = SimpleNamespace(text="hello", caption=None, entities=(), caption_entities=(), reply_text=AsyncMock())
    update = SimpleNamespace(effective_user=SimpleNamespace(id=111), message=message)
    await bot.handle_links(update, handler_context())
    message.reply_text.assert_awaited_once_with("Không tìm thấy link nào. Dùng: /up <link>")


async def test_links_from_stranger_are_ignored():
    message = SimpleNamespace(text="https://x", caption=None, entities=(), caption_entities=(), reply_text=AsyncMock())
    update = SimpleNamespace(effective_user=SimpleNamespace(id=999), message=message)
    await bot.handle_links(update, handler_context())
    message.reply_text.assert_not_awaited()


# --- Takeover by another machine ----------------------------------------------------------------------------------


def error_context(error):
    return SimpleNamespace(error=error, application=MagicMock())


async def test_conflict_after_grace_stops_bot(monkeypatch):
    monkeypatch.setattr(bot, "STARTED_AT", time.monotonic() - bot.TAKEOVER_GRACE - 1)
    context = error_context(Conflict("terminated by other getUpdates request"))
    await bot.on_error(None, context)
    context.application.stop_running.assert_called_once()


async def test_conflict_during_grace_keeps_running(monkeypatch):
    monkeypatch.setattr(bot, "STARTED_AT", time.monotonic())
    context = error_context(Conflict("terminated by other getUpdates request"))
    await bot.on_error(None, context)
    context.application.stop_running.assert_not_called()


async def test_other_errors_keep_running(monkeypatch):
    monkeypatch.setattr(bot, "STARTED_AT", time.monotonic() - bot.TAKEOVER_GRACE - 1)
    context = error_context(NetworkError("down"))
    await bot.on_error(None, context)
    context.application.stop_running.assert_not_called()


def test_build_app_registers_handlers():
    app = bot.build_app()
    assert app.error_handlers
    assert sum(len(h) for h in app.handlers.values()) == 5


async def test_pending_menus_are_capped(monkeypatch):
    monkeypatch.setattr(bot, "MAX_PENDING", 2)
    monkeypatch.setattr(bot, "classify_link", AsyncMock(return_value=("video", {})))
    status = SimpleNamespace(edit_text=AsyncMock())
    reply = AsyncMock(return_value=status)
    text = "https://a https://b https://c"
    message = SimpleNamespace(text=text, caption=None, entities=(), caption_entities=(), reply_text=reply)
    update = SimpleNamespace(effective_user=SimpleNamespace(id=111), message=message)
    context = handler_context()
    context.application = MagicMock()
    await bot.handle_links(update, context)
    assert list(context.bot_data["pending"].values()) == ["https://b", "https://c"]


# --- Link extraction ----------------------------------------------------------------------------------------------


def tg_message(text, entities=()):
    from telegram import MessageEntity

    ents = [MessageEntity(type=t, offset=o, length=n, url=u) for t, o, n, u in entities]
    return SimpleNamespace(text=text, entities=ents, caption=None, caption_entities=())


def test_extract_urls_accepts_links_without_scheme():
    text = "www.nhaccuatui.com/song/jPTfR8A5fJ4o"
    message = tg_message(text, [("url", 0, len(text), None)])
    assert bot.extract_urls(message) == ["https://www.nhaccuatui.com/song/jPTfR8A5fJ4o"]


def test_extract_urls_handles_emoji_offsets_and_text_links():
    text = "🎵 nghe x.com/a và bấm đây"  # The emoji is 2 UTF-16 units, so x.com starts at offset 8, not 7.
    message = tg_message(text, [("url", 8, 7, None), ("text_link", 25, 3, "https://y.com/b")])
    assert bot.extract_urls(message) == ["https://x.com/a", "https://y.com/b"]


def test_extract_urls_falls_back_to_regex_and_dedupes():
    message = tg_message("https://a.com/x https://a.com/x", [("url", 0, 15, None)])
    assert bot.extract_urls(message) == ["https://a.com/x"]


# --- NhacCuaTui ---------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "key"),
    [
        ("https://www.nhaccuatui.com/song/jPTfR8A5fJ4o", "jPTfR8A5fJ4o"),
        ("https://nhaccuatui.com/song/jPTfR8A5fJ4o?source=app", "jPTfR8A5fJ4o"),
        ("https://www.nhaccuatui.com/bai-hat/co-khong-giu-mat-dung-tim.jPTfR8A5fJ4o.html", "jPTfR8A5fJ4o"),
    ],
)
def test_nct_url(url, key):
    match = bot.NCT_RE.match(url)
    assert match and (match.group(1) or match.group(2)) == key


def test_nct_url_rejects_other_pages():
    assert not bot.NCT_RE.match("https://www.nhaccuatui.com/playlist/abc")
    assert not bot.NCT_RE.match("https://example.com/song/abc")


def nct_page(key="KEY1", vip320=False):
    """A NhacCuaTui page whose Nuxt payload holds a related song first and the requested song second."""
    import json

    data = [
        {"related": 1, "song": 9},
        {"key": 2, "name": 3, "artistName": 3, "duration": 4, "streamURL": 5},
        "OTHER", "Bài khác", "100", [6], {"stream": 7, "type": 8, "status": 17, "onlyVIP": 18}, "https://a/other.mp3",
        "128",
        {"key": 10, "name": 11, "artistName": 12, "duration": 13, "streamURL": 14},
        key, "Có Không Giữ Mất Đừng Tìm", "Cảnh Minh", "244", [15, 16, 19],
        {"stream": 20, "type": 8, "status": 17, "onlyVIP": 18},
        {"stream": 21, "type": 22, "status": 17, "onlyVIP": 23},
        1, False,
        {"stream": 24, "type": 25, "status": 17, "onlyVIP": 18},
        "https://a/song.mp3", "https://a/song_hq.mp3", "320", vip320, "https://a/song.flac", "lossless",
    ]
    return f'<html><script type="application/json" id="__NUXT_DATA__">{json.dumps(data)}</script></html>'


def test_nct_song_picks_requested_song_and_free_streams():
    song = bot.nct_song(nct_page(), "KEY1")
    assert (song["title"], song["artist"], song["duration"]) == ("Có Không Giữ Mất Đừng Tìm", "Cảnh Minh", 244)
    assert [s["type"] for s in song["streams"]] == ["128", "320", "lossless"]
    assert [s["type"] for s in bot.nct_song(nct_page(vip320=True), "KEY1")["streams"]] == ["128", "lossless"]


def test_nct_song_without_data():
    with pytest.raises(bot.yt_dlp.utils.DownloadError):
        bot.nct_song("<html></html>", "KEY1")


@pytest.mark.parametrize(("vip320", "quality", "stream"), [(False, 320, "song_hq.mp3"), (True, 128, "song.mp3")])
async def test_download_nct_prefers_320_kbps(monkeypatch, tmp_path, vip320, quality, stream):
    requested = []

    def handler(request):
        requested.append(request)
        if request.url.host == "www.nhaccuatui.com":
            return httpx.Response(200, text=nct_page(vip320=vip320))
        return httpx.Response(200, content=b"ID3 audio")

    mock_client(monkeypatch, handler)
    progress = bot.Progress(FakeStatus(), asyncio.get_running_loop())
    path, info = await bot.download_nct("https://www.nhaccuatui.com/song/KEY1", str(tmp_path), progress)
    assert path == tmp_path / "Cảnh Minh - Có Không Giữ Mất Đừng Tìm.mp3"
    assert path.read_bytes() == b"ID3 audio"
    assert info == {"title": "Có Không Giữ Mất Đừng Tìm", "artist": "Cảnh Minh", "duration": 244, "abr": quality}
    assert requested[1].url.path == f"/{stream}"
    assert requested[1].headers["referer"] == "https://www.nhaccuatui.com/"


async def test_nct_links_skip_ytdlp(monkeypatch):
    process_url = AsyncMock()
    classify = AsyncMock()
    monkeypatch.setattr(bot, "process_url", process_url)
    monkeypatch.setattr(bot, "classify_link", classify)
    status = SimpleNamespace(edit_text=AsyncMock())
    text = "www.nhaccuatui.com/song/KEY1"
    message = tg_message(text, [("url", 0, len(text), None)])
    message.reply_text = AsyncMock(return_value=status)
    update = SimpleNamespace(effective_user=SimpleNamespace(id=111), message=message)
    context = handler_context()
    await bot.handle_links(update, context)
    process_url.assert_awaited_once_with(context, status, "https://www.nhaccuatui.com/song/KEY1", "nct")
    classify.assert_not_awaited()
