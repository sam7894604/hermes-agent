import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import PlatformConfig


def _ensure_discord_mock():
    if "discord" in sys.modules and hasattr(sys.modules["discord"], "__file__"):
        return

    discord_mod = MagicMock()
    discord_mod.Intents.default.return_value = MagicMock()
    discord_mod.Client = MagicMock
    discord_mod.File = MagicMock
    discord_mod.DMChannel = type("DMChannel", (), {})
    discord_mod.Thread = type("Thread", (), {})
    discord_mod.ForumChannel = type("ForumChannel", (), {})
    discord_mod.ui = SimpleNamespace(View=object, button=lambda *a, **k: (lambda fn: fn), Button=object)
    discord_mod.ButtonStyle = SimpleNamespace(success=1, primary=2, secondary=2, danger=3, green=1, grey=2, blurple=2, red=3)
    discord_mod.Color = SimpleNamespace(orange=lambda: 1, green=lambda: 2, blue=lambda: 3, red=lambda: 4, purple=lambda: 5)
    discord_mod.Interaction = object
    discord_mod.Embed = MagicMock
    discord_mod.app_commands = SimpleNamespace(
        describe=lambda **kwargs: (lambda fn: fn),
        choices=lambda **kwargs: (lambda fn: fn),
        Choice=lambda **kwargs: SimpleNamespace(**kwargs),
    )

    ext_mod = MagicMock()
    commands_mod = MagicMock()
    commands_mod.Bot = MagicMock
    ext_mod.commands = commands_mod

    sys.modules.setdefault("discord", discord_mod)
    sys.modules.setdefault("discord.ext", ext_mod)
    sys.modules.setdefault("discord.ext.commands", commands_mod)


_ensure_discord_mock()

from plugins.platforms.discord.adapter import DiscordAdapter  # noqa: E402


def _voice_adapter(reference_obj, *, native_result=None, native_error=None):
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="***"))
    ref_msg = SimpleNamespace(id=99, to_reference=MagicMock(return_value=reference_obj))
    channel = SimpleNamespace(
        id=555,
        fetch_message=AsyncMock(return_value=ref_msg),
        send=AsyncMock(return_value=SimpleNamespace(id=888)),
    )
    request = AsyncMock(return_value=native_result or {"id": "777"})
    if native_error is not None:
        request.side_effect = native_error
    adapter._client = SimpleNamespace(
        get_channel=lambda _chat_id: channel,
        fetch_channel=AsyncMock(),
        http=SimpleNamespace(request=request),
    )
    return adapter, channel, request


def _native_voice_payload(request):
    form = request.await_args.kwargs["form"]
    payload = next(part["value"] for part in form if part["name"] == "payload_json")
    return json.loads(payload)


@pytest.mark.asyncio
async def test_send_retries_without_reference_when_reply_target_is_deleted():
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="***"))

    reference_obj = object()
    ref_msg = SimpleNamespace(id=99, to_reference=MagicMock(return_value=reference_obj))
    sent_msgs = [SimpleNamespace(id=1001), SimpleNamespace(id=1002)]
    send_calls = []

    async def fake_send(*, content, reference=None):
        send_calls.append({"content": content, "reference": reference})
        if len(send_calls) == 1:
            raise RuntimeError(
                "400 Bad Request (error code: 10008): Unknown Message"
            )
        return sent_msgs[len(send_calls) - 2]

    channel = SimpleNamespace(
        fetch_message=AsyncMock(return_value=ref_msg),
        send=AsyncMock(side_effect=fake_send),
    )
    adapter._client = SimpleNamespace(
        get_channel=lambda _chat_id: channel,
        fetch_channel=AsyncMock(),
    )

    long_text = "A" * (adapter.MAX_MESSAGE_LENGTH + 50)
    result = await adapter.send("555", long_text, reply_to="99")

    assert result.success is True
    assert result.message_id == "1001"
    assert channel.fetch_message.await_count == 1
    assert channel.send.await_count == 3
    ref_msg.to_reference.assert_called_once_with(fail_if_not_exists=False)
    assert send_calls[0]["reference"] is reference_obj
    assert send_calls[1]["reference"] is None
    assert send_calls[2]["reference"] is None


# ---------------------------------------------------------------------------
# Forum channel tests
# ---------------------------------------------------------------------------

import discord as _discord_mod  # noqa: E402 — imported after _ensure_discord_mock


class TestIsForumParent:
    def test_none_returns_false(self):
        adapter = DiscordAdapter(PlatformConfig(enabled=True, token="***"))
        assert adapter._is_forum_parent(None) is False

    def test_forum_channel_class_instance(self):
        adapter = DiscordAdapter(PlatformConfig(enabled=True, token="***"))
        forum_cls = getattr(_discord_mod, "ForumChannel", None)
        if forum_cls is None:
            # Re-create a type for the mock
            forum_cls = type("ForumChannel", (), {})
            _discord_mod.ForumChannel = forum_cls
        ch = forum_cls()
        assert adapter._is_forum_parent(ch) is True


# ---------------------------------------------------------------------------
# Forum follow-up chunk failure reporting + media on forum paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_forum_post_file_creates_thread_with_attachment():
    """_forum_post_file routes file-bearing sends to create_thread with file kwarg."""
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="***"))

    thread_ch = SimpleNamespace(id=777, send=AsyncMock())
    thread = SimpleNamespace(
        id=777,
        message=SimpleNamespace(
            id=800,
            attachments=[SimpleNamespace(filename="photo.png")],
        ),
        thread=thread_ch,
    )
    forum_channel = _discord_mod.ForumChannel()
    forum_channel.id = 999
    forum_channel.name = "ideas"
    forum_channel.create_thread = AsyncMock(return_value=thread)

    # discord.File is a real class; build a MagicMock that looks like one
    fake_file = SimpleNamespace(filename="photo.png")

    result = await adapter._forum_post_file(
        forum_channel,
        content="here is a photo",
        file=fake_file,
    )

    assert result.success is True
    assert result.message_id == "800"
    forum_channel.create_thread.assert_awaited_once()
    call_kwargs = forum_channel.create_thread.await_args.kwargs
    assert call_kwargs["file"] is fake_file
    assert call_kwargs["content"] == "here is a photo"
    # Thread name derived from content's first line
    assert call_kwargs["name"] == "here is a photo"


@pytest.mark.asyncio
async def test_forum_post_file_fails_when_starter_has_no_attachments():
    """Forum create_thread can succeed yet return an attachmentless starter (#66797)."""
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="***"))

    thread = SimpleNamespace(
        id=7,
        message=SimpleNamespace(id=8, attachments=[]),
        thread=SimpleNamespace(id=7, send=AsyncMock()),
    )
    forum_channel = _discord_mod.ForumChannel()
    forum_channel.id = 999
    forum_channel.create_thread = AsyncMock(return_value=thread)

    fake_file = SimpleNamespace(filename="clip.mp4")
    result = await adapter._forum_post_file(
        forum_channel,
        content="video clip",
        files=[fake_file],
    )

    assert result.success is False
    assert "no files" in (result.error or "").lower()
    forum_channel.create_thread.assert_awaited_once()


# ---------------------------------------------------------------------------
# Typing indicator task lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_typing_restartable_after_error():
    """After a typing error, send_typing should start a new task (not blocked by stale entry)."""
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="***"))
    adapter._client = MagicMock()
    adapter._client.http = MagicMock()
    adapter._typing_tasks = {}

    # First call fails
    adapter._client.http.request = AsyncMock(side_effect=Exception("503"))
    await adapter.send_typing("12345")
    await asyncio.sleep(0.1)

    # Second call should work
    adapter._client.http.request = AsyncMock()
    await adapter.send_typing("12345")

    assert "12345" in adapter._typing_tasks, \
        "Should restart typing after previous failure"


# ---------------------------------------------------------------------------
# #66797 — outbound MEDIA video must reach channel.send as a real attachment
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_video_uses_path_based_files_kwarg(tmp_path, monkeypatch):
    """Regression for #66797: video MEDIA delivery must use path-based
    ``discord.File`` via ``files=[...]`` (same pattern as image batching).

    The previous open-handle + singular ``file=`` form could return a successful
    message with zero attachments after an earlier image batch on the same
    channel — silent drop from the user's perspective.
    """
    import plugins.platforms.discord.adapter as discord_platform

    video = tmp_path / "clip.mp4"
    video.write_bytes(b"\x00\x00\x00\x18ftypmp42fake")

    captured = {}

    class _FakeFile:
        def __init__(self, fp, filename=None, **kwargs):
            captured["fp"] = fp
            captured["filename"] = filename

    monkeypatch.setattr(discord_platform.discord, "File", _FakeFile)

    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="***"))
    sent_msg = SimpleNamespace(
        id=4242,
        attachments=[SimpleNamespace(filename="clip.mp4", url="https://cdn.example/clip.mp4")],
    )
    channel = SimpleNamespace(
        send=AsyncMock(return_value=sent_msg),
        type=0,
    )
    adapter._client = SimpleNamespace(
        get_channel=lambda _chat_id: channel,
        fetch_channel=AsyncMock(),
    )
    monkeypatch.setattr(adapter, "_is_forum_parent", lambda _ch: False)

    result = await adapter.send_video("555", str(video))

    assert result.success is True
    assert result.message_id == "4242"
    assert captured["fp"] == str(video)
    assert captured["filename"] == "clip.mp4"
    channel.send.assert_awaited_once()
    send_kwargs = channel.send.await_args.kwargs
    assert send_kwargs.get("file") is None
    assert isinstance(send_kwargs.get("files"), list) and len(send_kwargs["files"]) == 1


@pytest.mark.asyncio
async def test_send_video_fails_loud_when_message_has_no_attachments(tmp_path, monkeypatch):
    """If Discord accepts the message but attaches nothing, fail loud (#66797)."""
    import plugins.platforms.discord.adapter as discord_platform

    video = tmp_path / "clip.mp4"
    video.write_bytes(b"fake-mp4")

    monkeypatch.setattr(
        discord_platform.discord,
        "File",
        lambda fp, filename=None, **kwargs: SimpleNamespace(fp=fp, filename=filename),
    )

    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="***"))
    # Message id present, but no attachments — the silent-drop failure mode.
    sent_msg = SimpleNamespace(id=99, attachments=[])
    channel = SimpleNamespace(send=AsyncMock(return_value=sent_msg), type=0)
    adapter._client = SimpleNamespace(
        get_channel=lambda _chat_id: channel,
        fetch_channel=AsyncMock(),
    )
    monkeypatch.setattr(adapter, "_is_forum_parent", lambda _ch: False)

    result = await adapter.send_video("555", str(video))

    assert result.success is False
    assert "no files" in (result.error or "").lower()
    channel.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_send_video_missing_file_fails_fast_without_touching_channel():
    """A missing MEDIA path must fail loud before any Discord I/O (#66797).

    The pre-flight ``os.path.isfile`` guard turns a would-be crash inside
    ``discord.File`` into an actionable ``File not found`` result, and must
    short-circuit before the channel is ever resolved.
    """
    def _boom(*_args, **_kwargs):
        raise AssertionError("channel must not be resolved for a missing file")

    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="***"))
    adapter._client = SimpleNamespace(get_channel=_boom, fetch_channel=AsyncMock(side_effect=_boom))

    result = await adapter.send_video("555", "/no/such/clip.mp4")

    assert result.success is False
    assert "not found" in (result.error or "").lower()


@pytest.mark.asyncio
async def test_send_file_attachment_forum_uses_files_kwarg(tmp_path, monkeypatch):
    """Forum-parent delivery must also route the path-based file through the
    plural ``files=[...]`` kwarg (#66797), so the create_thread starter message
    carries the attachment rather than silently dropping it."""
    import plugins.platforms.discord.adapter as discord_platform

    video = tmp_path / "clip.mp4"
    video.write_bytes(b"fake-mp4")

    monkeypatch.setattr(
        discord_platform.discord,
        "File",
        lambda fp, filename=None, **kwargs: SimpleNamespace(fp=fp, filename=filename),
    )

    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="***"))
    created_thread = SimpleNamespace(
        id=7,
        message=SimpleNamespace(
            id=8,
            attachments=[SimpleNamespace(filename="clip.mp4")],
        ),
    )
    forum_channel = SimpleNamespace(
        id=7,
        create_thread=AsyncMock(return_value=created_thread),
    )
    adapter._client = SimpleNamespace(
        get_channel=lambda _chat_id: forum_channel,
        fetch_channel=AsyncMock(),
    )
    monkeypatch.setattr(adapter, "_is_forum_parent", lambda _ch: True)

    result = await adapter.send_video("555", str(video))

    assert result.success is True
    forum_channel.create_thread.assert_awaited_once()
    thread_kwargs = forum_channel.create_thread.await_args.kwargs
    assert thread_kwargs.get("file") is None
    assert isinstance(thread_kwargs.get("files"), list) and len(thread_kwargs["files"]) == 1




# ---------------------------------------------------------------------------
# Markdown table → aligned monospace code block
# ---------------------------------------------------------------------------


def _code_block_lines(text):
    """Return the lines inside the first ``` fenced block in *text*."""
    assert "```" in text
    inner = text.split("```")[1]
    return [ln for ln in inner.split("\n") if ln != ""]


def _east_asian_wide(ch):
    import unicodedata
    return unicodedata.east_asian_width(ch) in ("W", "F")


class TestConvertTablesToCodeBlocks:
    def test_tiny_table_exact_alignment(self):
        # Widths: col0=1 ("A"/"1"), col1=2 ("B"/"22"). Pipes must line up.
        text = "| A | B |\n|---|---|\n| 1 | 22 |"
        assert DiscordAdapter._convert_tables_to_code_blocks(text) == (
            "```\nA | B\n--|---\n1 | 22\n```"
        )

    def test_table_wrapped_inline_and_columns_aligned(self):
        text = (
            "Here is the data:\n\n"
            "| Name | Age | City |\n"
            "|------|-----|------|\n"
            "| Alice | 30 | NYC |\n"
            "| Bob | 25 | LA |\n\n"
            "Done."
        )
        out = DiscordAdapter._convert_tables_to_code_blocks(text)
        # Prose kept inline around the block (not detached).
        assert out.startswith("Here is the data:\n\n```\n")
        assert out.endswith("\n```\n\nDone.")
        assert "Alice" in out and "Bob" in out
        # Every line inside the block has its pipes at identical columns.
        lines = _code_block_lines(out)
        pipe_cols = [tuple(i for i, ch in enumerate(ln) if ch == "|") for ln in lines]
        assert len(set(pipe_cols)) == 1
        assert pipe_cols[0]  # at least one pipe column

    def test_table_inside_code_fence_is_preserved(self):
        text = (
            "Look:\n```\n"
            "| not | a | table |\n| --- | --- | --- |\n| 1 | 2 | 3 |\n"
            "```\nafter."
        )
        # Already fenced → untouched.
        assert DiscordAdapter._convert_tables_to_code_blocks(text) == text

    def test_pipes_in_prose_are_not_converted(self):
        text = "Pipe the | output | here.\nAnd another | one."
        assert DiscordAdapter._convert_tables_to_code_blocks(text) == text

    def test_ragged_rows_padded_and_truncated(self):
        text = (
            "| a | b | c |\n|---|---|---|\n"
            "| 1 | 2 |\n"          # short row → padded
            "| 3 | 4 | 5 | 6 |"    # long row → extra cell dropped
        )
        out = DiscordAdapter._convert_tables_to_code_blocks(text)
        lines = _code_block_lines(out)
        # Header + separator + 2 data rows, 3 columns each (2 pipes/line).
        assert len(lines) == 4
        assert all(ln.count("|") == 2 for ln in lines)
        assert "6" not in out  # 4th cell of the long row was dropped

    def test_two_tables_both_wrapped_with_prose_between(self):
        text = (
            "| A | B |\n|---|---|\n| 1 | 2 |\n\n"
            "middle\n\n"
            "| C | D |\n|---|---|\n| 9 | 8 |"
        )
        out = DiscordAdapter._convert_tables_to_code_blocks(text)
        assert out.count("```") == 4  # two fenced blocks (open+close each)
        assert "\n\nmiddle\n\n" in out

    def test_cjk_columns_align_by_display_width(self):
        # CJK glyphs render 2 cells wide in Discord's monospace font. Padding
        # must use display width, not len(), or CJK columns drift.
        text = (
            "| 項目 | 叔鼠 | 寶寶 |\n|---|---|---|\n"
            "| 稱呼 | 叔鼠、Sam Liu | 玄兒 |\n"
            "| 人格特質 | 策略、效率 | INFJ |"
        )
        out = DiscordAdapter._convert_tables_to_code_blocks(text)
        lines = _code_block_lines(out)

        def pipe_cols(line):
            # Display-column index of each '|' (W/F chars count as 2).
            cols, acc = [], 0
            for ch in line:
                if ch == "|":
                    cols.append(acc)
                acc += 2 if _east_asian_wide(ch) else 1
            return tuple(cols)

        positions = {pipe_cols(ln) for ln in lines}
        assert len(positions) == 1  # every row's pipes align by display width
        assert next(iter(positions))  # and there is at least one pipe column

    def test_display_width_counts_cjk_as_two(self):
        assert DiscordAdapter._display_width("項目") == 4
        assert DiscordAdapter._display_width("abc") == 3
        assert DiscordAdapter._display_width("A項") == 3

    def test_code_block_strips_markdown_markers(self):
        # **bold** must not appear literally, and columns align on the
        # visible ("¥0.04") text, not the marker-laden source.
        text = "| a | b |\n|---|---|\n| x | **¥0.04** |"
        out = DiscordAdapter._convert_tables_to_code_blocks(text)
        assert "**" not in out
        assert "¥0.04" in out


class TestInlineMarkdown:
    def test_bold_run(self):
        assert DiscordAdapter._parse_inline_md("**hi**") == [("hi", frozenset({"b"}))]

    def test_mixed_runs(self):
        runs = DiscordAdapter._parse_inline_md("a **b** c")
        assert runs == [
            ("a ", frozenset()),
            ("b", frozenset({"b"})),
            (" c", frozenset()),
        ]

    def test_bold_italic_and_code_and_strike(self):
        assert DiscordAdapter._parse_inline_md("***x***") == [("x", frozenset({"b", "i"}))]
        assert DiscordAdapter._parse_inline_md("`c`") == [("c", frozenset({"c"}))]
        assert DiscordAdapter._parse_inline_md("~~s~~") == [("s", frozenset({"s"}))]

    def test_unmatched_marker_kept_literal(self):
        assert DiscordAdapter._parse_inline_md("a * b") == [("a * b", frozenset())]

    def test_strip_inline_md(self):
        assert DiscordAdapter._strip_inline_md("**¥0.04**") == "¥0.04"
        assert DiscordAdapter._strip_inline_md("plain") == "plain"

    def test_no_pipes_returns_unchanged_identity(self):
        text = "just a normal sentence"
        assert DiscordAdapter._convert_tables_to_code_blocks(text) is text

    def test_format_message_wraps_tables(self):
        adapter = DiscordAdapter(PlatformConfig(enabled=True, token="***"))
        out = adapter.format_message("| A | B |\n|---|---|\n| 1 | 2 |")
        assert out.startswith("```\n") and out.endswith("\n```")


# ---------------------------------------------------------------------------
# Message splitting (text / table parts) + inline table image
# ---------------------------------------------------------------------------


class TestSplitMessageParts:
    def test_no_table_single_text_part(self):
        assert DiscordAdapter._split_message_parts("hello world") == [
            {"type": "text", "text": "hello world"}
        ]

    def test_table_between_prose_splits_in_order(self):
        parts = DiscordAdapter._split_message_parts(
            "intro\n| A | B |\n|---|---|\n| 1 | 2 |\nouttro"
        )
        assert [p["type"] for p in parts] == ["text", "table", "text"]
        assert parts[0]["text"].strip() == "intro"
        assert parts[1]["header"] == "| A | B |"
        assert parts[1]["rows"] == ["| 1 | 2 |"]
        assert parts[2]["text"].strip() == "outtro"

    def test_table_inside_code_fence_stays_text(self):
        text = "```\n| A | B |\n|---|---|\n| 1 | 2 |\n```"
        parts = DiscordAdapter._split_message_parts(text)
        assert [p["type"] for p in parts] == ["text"]

    def test_pipes_in_prose_stay_text(self):
        parts = DiscordAdapter._split_message_parts("use the | pipe | here")
        assert [p["type"] for p in parts] == ["text"]

    def test_two_tables(self):
        parts = DiscordAdapter._split_message_parts(
            "| A |\n|---|\n| 1 |\n\nmid\n\n| B |\n|---|\n| 2 |"
        )
        assert [p["type"] for p in parts] == ["table", "text", "table"]
        assert parts[1]["text"].strip() == "mid"


@pytest.mark.asyncio
async def test_send_splits_table_into_inline_image(monkeypatch):
    """A table renders to a PNG that is sent inline between the surrounding
    prose: text-before → image → text-after, in order."""
    monkeypatch.setattr(
        DiscordAdapter, "_render_table_image", classmethod(lambda cls, h, d: b"PNGDATA")
    )
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="***"))

    sent = []

    async def fake_send(*, content=None, file=None, reference=None):
        sent.append({"content": content, "file": file})
        return SimpleNamespace(id=len(sent))

    channel = SimpleNamespace(
        send=AsyncMock(side_effect=fake_send),
        fetch_message=AsyncMock(),
    )
    adapter._client = SimpleNamespace(
        get_channel=lambda _chat_id: channel,
        fetch_channel=AsyncMock(),
    )

    body = "intro\n\n| A | B |\n|---|---|\n| 1 | 2 |\n\nouttro"
    result = await adapter.send("123", body)

    assert result.success is True
    assert len(sent) == 3
    assert sent[0]["content"] == "intro" and sent[0]["file"] is None
    assert sent[1]["file"] is not None and sent[1]["content"] is None  # the image
    assert sent[2]["content"] == "outtro" and sent[2]["file"] is None


@pytest.mark.asyncio
async def test_send_falls_back_to_code_block_when_no_image(monkeypatch):
    """When image rendering is unavailable, the table is sent inline as an
    aligned code block in a single message (original behaviour)."""
    monkeypatch.setattr(
        DiscordAdapter, "_render_table_image", classmethod(lambda cls, h, d: None)
    )
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="***"))

    sent = []

    async def fake_send(*, content=None, file=None, reference=None):
        sent.append({"content": content, "file": file})
        return SimpleNamespace(id=len(sent))

    channel = SimpleNamespace(
        send=AsyncMock(side_effect=fake_send),
        fetch_message=AsyncMock(),
    )
    adapter._client = SimpleNamespace(
        get_channel=lambda _chat_id: channel,
        fetch_channel=AsyncMock(),
    )

    result = await adapter.send("123", "intro\n\n| A | B |\n|---|---|\n| 1 | 22 |\n\nouttro")

    assert result.success is True
    assert len(sent) == 1  # single inline message
    assert sent[0]["file"] is None
    assert sent[0]["content"] == "intro\n\n```\nA | B\n--|---\n1 | 22\n```\n\nouttro"


def test_render_table_image_produces_png_when_font_available():
    """If a CJK/Latin font is present, rendering yields real PNG bytes;
    otherwise it returns None (and callers fall back to text)."""
    font, _ = DiscordAdapter._load_table_fonts(DiscordAdapter._TABLE_IMG_FONT_SIZE)
    png = DiscordAdapter._render_table_image("| 項目 | 值 |", ["| 稱呼 | 叔鼠 |"])
    if font is None:
        assert png is None
    else:
        assert png is not None and png[:8] == b"\x89PNG\r\n\x1a\n"
