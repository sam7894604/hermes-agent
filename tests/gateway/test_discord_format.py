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


