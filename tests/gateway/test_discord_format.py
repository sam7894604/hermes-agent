"""Discord format_message: tables converted to bullet groups."""

import types
import sys


def _make_discord_adapter():
    """Construct a DiscordAdapter with discord.py stubbed out."""
    fake_discord = types.ModuleType("discord")
    fake_discord.Intents = type("Intents", (), {"default": classmethod(lambda cls: cls())})
    fake_discord.Message = object
    fake_ext = types.ModuleType("discord.ext")
    fake_commands = types.ModuleType("discord.ext.commands")
    fake_ext.commands = fake_commands
    fake_discord.ext = fake_ext
    sys.modules.setdefault("discord", fake_discord)
    sys.modules.setdefault("discord.ext", fake_ext)
    sys.modules.setdefault("discord.ext.commands", fake_commands)

    from plugins.platforms.discord.adapter import DiscordAdapter
    adapter = object.__new__(DiscordAdapter)
    return adapter


class TestDiscordFormatMessage:

    def test_table_rendered_as_code_block(self):
        # This fork branch's table→image feature supersedes upstream's
        # convert_table_to_bullets: format_message (the text fallback used when
        # image rendering is unavailable) re-renders each table as an aligned
        # monospace code block, keeping it inline within the message.
        adapter = _make_discord_adapter()
        text = (
            "Results:\n\n"
            "| Name | Score |\n"
            "|------|-------|\n"
            "| Alice | 95   |\n"
            "| Bob   | 80   |\n"
            "\nDone."
        )
        out = adapter.format_message(text)
        assert "```" in out                       # rendered as a code block
        assert "Alice" in out and "95" in out      # data preserved
        assert "Bob" in out and "80" in out
        assert out.startswith("Results:")
        assert out.rstrip().endswith("Done.")
        assert "**Alice**" not in out              # not the old bullet form


class TestDiscordToolPreviewFormatting:
    def test_truncated_url_keeps_full_click_target(self):
        from agent.display import ToolPreview

        adapter = _make_discord_adapter()
        url = "https://hermes-agent.nousresearch.com/docs/gateway/discord/tool-progress"
        visible = "https://hermes-agent.nousresearch..."

        out = adapter.format_tool_preview(ToolPreview(visible, truncated=True, url=url))

        assert out == f"[hermes-agent.nousresearch...](<{url}>)"

    def test_truncated_url_label_is_not_a_second_url_target(self):
        from agent.display import ToolPreview

        adapter = _make_discord_adapter()
        url = "https://centaur.run/secrets/advanced-permissioning"
        visible = "https://centaur.run/secrets/advanced-..."

        out = adapter.format_tool_preview(ToolPreview(visible, truncated=True, url=url))

        assert out == (
            "[centaur.run/secrets/advanced-...]"
            "(<https://centaur.run/secrets/advanced-permissioning>)"
        )

    def test_link_escapes_discord_markdown_delimiters(self):
        from agent.display import ToolPreview

        adapter = _make_discord_adapter()
        preview = ToolPreview(
            r"https://example.com/docs/[beta]...",
            truncated=True,
            url="https://example.com/docs/_(beta)",
        )

        assert adapter.format_tool_preview(preview) == (
            r"[example.com/docs/\[beta\]...]"
            r"(<https://example.com/docs/_%28beta%29>)"
        )

    def test_structured_tool_event_uses_clickable_truncated_url(self):
        from gateway.stream_events import ToolCallChunk

        adapter = _make_discord_adapter()
        url = "https://hermes-agent.nousresearch.com/docs/gateway/discord/tool-progress"
        visible = url[:37] + "..."

        out = adapter.format_tool_event(
            ToolCallChunk("web_extract", preview=url, args={"urls": [url]}),
            mode="all",
            preview_max_len=40,
        )

        assert out is not None
        assert f"[{visible.removeprefix('https://')}](<{url}>)" in out

    def test_untruncated_url_remains_plain(self):
        from agent.display import ToolPreview

        adapter = _make_discord_adapter()
        url = "https://example.com/page"

        out = adapter.format_tool_preview(ToolPreview(url))

        assert out == url

    def test_truncated_non_url_remains_plain(self):
        from agent.display import ToolPreview

        adapter = _make_discord_adapter()
        visible = "a long search query that was trunc..."

        out = adapter.format_tool_preview(ToolPreview(visible, truncated=True))

        assert out == visible
