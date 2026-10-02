"""Discord override: ``send()`` appends auto-choice buttons only after a successful conversational send, and
never posts them into a forum parent channel (upstream turned that reply into a thread post)."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import PlatformConfig
from gateway.platforms.base import SendResult
from tests.fork_plugins._plugin_loader import load_fork_plugin

_discord = load_fork_plugin("platforms/discord").adapter

PROMPT = "? Which plan?\n1. Free\n2. Pro"


def _adapter():
    ad = _discord.DiscordAdapter(PlatformConfig(enabled=True, token="t", extra={}))
    ad._client = MagicMock()
    ad._allowed_user_ids = {"42"}
    ad._allowed_role_ids = set()
    return ad


@pytest.mark.asyncio
async def test_send_appends_buttons_only_for_successful_conversational_sends(monkeypatch):
    async def _upstream_send(self, chat_id, content, reply_to=None, metadata=None):
        return SendResult(success=bool(content), message_id="1" if content else None)

    monkeypatch.setattr(_discord._base.DiscordAdapter, "send", _upstream_send)
    ad = _adapter()
    seen = []
    ad._maybe_send_choice_buttons = AsyncMock(side_effect=lambda *a: seen.append(a))

    assert (await ad.send("1", PROMPT, None, {"thread_id": "9"})).success
    assert seen == [("1", PROMPT, None, {"thread_id": "9"})]

    status_key = next(iter(_discord._base._DISCORD_NONCONVERSATIONAL_METADATA_KEYS))
    assert (await ad.send("1", PROMPT, metadata={status_key: True})).success
    assert len(seen) == 1  # status-only sends are not questions to the user

    assert not (await ad.send("1", "")).success
    assert len(seen) == 1  # nothing was delivered, so nothing to attach buttons to


@pytest.mark.asyncio
async def test_forum_parent_channel_gets_no_buttons():
    ad = _adapter()
    channel = MagicMock()
    channel.send = AsyncMock(return_value=MagicMock())
    ad._client.get_channel = MagicMock(return_value=channel)

    ad._is_forum_parent = MagicMock(return_value=True)
    await ad._maybe_send_choice_buttons("1", PROMPT, None, None)
    channel.send.assert_not_called()

    ad._is_forum_parent = MagicMock(return_value=False)
    await ad._maybe_send_choice_buttons("1", PROMPT, None, None)
    channel.send.assert_awaited_once()
    assert isinstance(channel.send.call_args.kwargs["view"], _discord.AutoChoiceView)
