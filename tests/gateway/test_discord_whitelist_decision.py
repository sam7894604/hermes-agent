"""Tests for the Discord interactive LINE whitelist-decision card.

Covers ``WhitelistDecisionView`` (Approve/Ignore/Skip button handlers hitting
the shared ``WhitelistStore``, with an admin gate) and the adapter's
``send_whitelist_decision`` sender. Uses the shared discord mock from
``tests/gateway/conftest.py``.
"""

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_repo = str(Path(__file__).resolve().parents[2])
if _repo not in sys.path:
    sys.path.insert(0, _repo)

# Importing the adapter triggers the shared discord mock (conftest).
from plugins.platforms.discord.adapter import (  # noqa: E402
    DiscordAdapter,
    WhitelistDecisionView,
)
from gateway.config import PlatformConfig  # noqa: E402
import discord  # noqa: E402  (mocked via conftest)


def _make_adapter(*, allowed_users=None):
    config = PlatformConfig(enabled=True, token="test-token", extra={})
    adapter = DiscordAdapter(config)
    adapter._client = MagicMock()
    adapter._allowed_user_ids = set(allowed_users or ["42"])
    adapter._allowed_role_ids = set()
    return adapter


def _make_interaction(*, user_id="42", display_name="Admin"):
    user = SimpleNamespace(id=user_id, name=display_name,
                           display_name=display_name, bot=False, roles=[])
    response = SimpleNamespace(edit_message=AsyncMock(), send_message=AsyncMock())
    message = SimpleNamespace(embeds=[discord.Embed(title="x")])
    return SimpleNamespace(user=user, response=response, message=message)


class _FakeStore:
    def __init__(self, *, admin=True, approved=True, ignore_ok=True, scope="dm"):
        self._admin = admin
        self._approved = approved
        self._ignore_ok = ignore_ok
        self._scope = scope
        self.calls = []

    def is_admin(self, user_id):
        self.calls.append(("is_admin", user_id))
        return self._admin

    def approve_pending(self, source_id, *, added_by=""):
        self.calls.append(("approve_pending", source_id, added_by))
        return {"approved": self._approved, "scope": self._scope, "id": source_id}

    def ignore_pending(self, source_id):
        self.calls.append(("ignore_pending", source_id))
        return self._ignore_ok


def _patch_store(store):
    return patch(
        "plugins.platforms.line.whitelist_store.WhitelistStore",
        return_value=store,
    )


def _make_view():
    return WhitelistDecisionView(
        source_type="group", source_id="Cabc123",
        allowed_user_ids={"42"}, allowed_role_ids=set(),
    )


class TestWhitelistDecisionView:
    @pytest.mark.asyncio
    async def test_approve_calls_store(self):
        view = _make_view()
        interaction = _make_interaction()
        store = _FakeStore(admin=True, approved=True, scope="group")
        with _patch_store(store):
            await view.approve(interaction, None)
        assert ("approve_pending", "Cabc123", "42") in store.calls
        assert view.resolved is True
        interaction.response.edit_message.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_ignore_calls_store(self):
        view = _make_view()
        interaction = _make_interaction()
        store = _FakeStore(admin=True, ignore_ok=True)
        with _patch_store(store):
            await view.ignore(interaction, None)
        assert ("ignore_pending", "Cabc123") in store.calls
        assert view.resolved is True

    @pytest.mark.asyncio
    async def test_skip_is_noop(self):
        view = _make_view()
        interaction = _make_interaction()
        store = _FakeStore(admin=True)
        with _patch_store(store):
            await view.skip(interaction, None)
        assert not any(
            c[0] in ("approve_pending", "ignore_pending") for c in store.calls
        )
        assert view.resolved is True
        interaction.response.edit_message.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_non_admin_rejected(self):
        view = _make_view()
        interaction = _make_interaction()
        store = _FakeStore(admin=False)
        with _patch_store(store):
            await view.approve(interaction, None)
        assert not any(c[0] == "approve_pending" for c in store.calls)
        assert view.resolved is False
        interaction.response.send_message.assert_awaited()  # ephemeral reject

    @pytest.mark.asyncio
    async def test_unauthorized_user_rejected(self):
        view = _make_view()
        interaction = _make_interaction(user_id="999")  # not in allowlist
        store = _FakeStore(admin=True)
        with _patch_store(store):
            await view.approve(interaction, None)
        assert not any(c[0] == "approve_pending" for c in store.calls)
        assert view.resolved is False

    @pytest.mark.asyncio
    async def test_line_plugin_absent_degrades(self):
        view = _make_view()
        interaction = _make_interaction()
        with patch(
            "plugins.platforms.line.whitelist_store.WhitelistStore",
            side_effect=ImportError("line plugin missing"),
        ):
            await view.approve(interaction, None)
        # degrades: ephemeral "unavailable", never resolves the card
        assert view.resolved is False
        interaction.response.send_message.assert_awaited()


class TestSendWhitelistDecision:
    @pytest.mark.asyncio
    async def test_sends_view(self):
        adapter = _make_adapter()
        channel = MagicMock()
        sent = SimpleNamespace(id=123)
        channel.send = AsyncMock(return_value=sent)
        adapter._client.get_channel = MagicMock(return_value=channel)

        res = await adapter.send_whitelist_decision(
            chat_id="555", source_type="group", source_id="Cabc123", name="Team",
        )
        assert res.success
        assert res.message_id == "123"
        _, kwargs = channel.send.call_args
        assert isinstance(kwargs["view"], WhitelistDecisionView)
        assert kwargs["view"].source_id == "Cabc123"
