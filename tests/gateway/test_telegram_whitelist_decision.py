"""Tests for the Telegram interactive LINE whitelist-decision card.

Covers ``send_whitelist_decision`` (inline keyboard build) and the
``linewl:`` branch of ``_handle_callback_query`` (store dispatch + admin gate).
"""

import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_repo = str(Path(__file__).resolve().parents[2])
if _repo not in sys.path:
    sys.path.insert(0, _repo)


def _ensure_telegram_mock():
    if "telegram" in sys.modules and hasattr(sys.modules["telegram"], "__file__"):
        return
    mod = MagicMock()
    mod.ext.ContextTypes.DEFAULT_TYPE = type(None)
    mod.constants.ParseMode.MARKDOWN = "Markdown"
    mod.constants.ParseMode.MARKDOWN_V2 = "MarkdownV2"
    mod.constants.ParseMode.HTML = "HTML"
    mod.constants.ChatType.PRIVATE = "private"
    mod.constants.ChatType.GROUP = "group"
    mod.constants.ChatType.SUPERGROUP = "supergroup"
    mod.constants.ChatType.CHANNEL = "channel"
    mod.error.NetworkError = type("NetworkError", (OSError,), {})
    mod.error.TimedOut = type("TimedOut", (OSError,), {})
    mod.error.BadRequest = type("BadRequest", (Exception,), {})
    for name in ("telegram", "telegram.ext", "telegram.constants", "telegram.request"):
        sys.modules.setdefault(name, mod)
    sys.modules.setdefault("telegram.error", mod.error)


_ensure_telegram_mock()

from plugins.platforms.telegram.adapter import TelegramAdapter  # noqa: E402
from gateway.config import PlatformConfig  # noqa: E402


def _make_adapter():
    config = PlatformConfig(enabled=True, token="test-token", extra={})
    adapter = TelegramAdapter(config)
    adapter._bot = AsyncMock()
    adapter._app = MagicMock()
    return adapter


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


def _make_query(data, user_id="999"):
    query = AsyncMock()
    query.data = data
    query.message = MagicMock()
    query.message.chat_id = 555
    query.from_user = MagicMock()
    query.from_user.id = user_id
    query.from_user.first_name = "Admin"
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    update = MagicMock()
    update.callback_query = query
    return update, query


class TestSendWhitelistDecision:
    @pytest.mark.asyncio
    async def test_sends_three_button_keyboard(self):
        adapter = _make_adapter()
        mock_msg = MagicMock()
        mock_msg.message_id = 7
        adapter._send_message_with_thread_fallback = AsyncMock(return_value=mock_msg)

        # Capture the InlineKeyboardButton callback_data values. Under the test
        # harness InlineKeyboardMarkup is a MagicMock, so inspect the button
        # constructor calls directly (real class isn't imported here).
        import plugins.platforms.telegram.adapter as tg
        with patch.object(tg, "InlineKeyboardButton") as MockBtn, \
                patch.object(tg, "InlineKeyboardMarkup") as MockMarkup:
            res = await adapter.send_whitelist_decision(
                chat_id="555", source_type="group", source_id="Cabc123", name="Team",
            )
        assert res.success
        datas = [c.kwargs["callback_data"] for c in MockBtn.call_args_list]
        assert datas == [
            "linewl:approve:group:Cabc123",
            "linewl:ignore:group:Cabc123",
            "linewl:skip:group:Cabc123",
        ]
        # callback_data must stay under Telegram's 64-byte limit
        assert all(len(d.encode()) < 64 for d in datas)
        MockMarkup.assert_called_once()


class TestWhitelistCallback:
    @pytest.mark.asyncio
    async def test_approve_calls_store(self):
        adapter = _make_adapter()
        store = _FakeStore(admin=True, approved=True, scope="group")
        update, query = _make_query("linewl:approve:group:Cabc123")
        with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "*"}, clear=False):
            with patch(
                "plugins.platforms.line.whitelist_store.WhitelistStore",
                return_value=store,
            ):
                await adapter._handle_callback_query(update, MagicMock())
        assert ("approve_pending", "Cabc123", "999") in store.calls
        query.answer.assert_called_once()
        query.edit_message_text.assert_called_once()

    @pytest.mark.asyncio
    async def test_ignore_calls_store(self):
        adapter = _make_adapter()
        store = _FakeStore(admin=True, ignore_ok=True)
        update, query = _make_query("linewl:ignore:dm:Uabc123")
        with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "*"}, clear=False):
            with patch(
                "plugins.platforms.line.whitelist_store.WhitelistStore",
                return_value=store,
            ):
                await adapter._handle_callback_query(update, MagicMock())
        assert ("ignore_pending", "Uabc123") in store.calls

    @pytest.mark.asyncio
    async def test_skip_is_noop(self):
        adapter = _make_adapter()
        store = _FakeStore(admin=True)
        update, query = _make_query("linewl:skip:room:Rabc123")
        with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "*"}, clear=False):
            with patch(
                "plugins.platforms.line.whitelist_store.WhitelistStore",
                return_value=store,
            ):
                await adapter._handle_callback_query(update, MagicMock())
        # skip must not mutate the store
        assert not any(c[0] in ("approve_pending", "ignore_pending") for c in store.calls)
        query.edit_message_text.assert_called_once()

    @pytest.mark.asyncio
    async def test_non_admin_rejected(self):
        adapter = _make_adapter()
        store = _FakeStore(admin=False)
        update, query = _make_query("linewl:approve:group:Cabc123")
        with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "*"}, clear=False):
            with patch(
                "plugins.platforms.line.whitelist_store.WhitelistStore",
                return_value=store,
            ):
                await adapter._handle_callback_query(update, MagicMock())
        # non-admin: no mutation, no message edit
        assert not any(c[0] == "approve_pending" for c in store.calls)
        query.edit_message_text.assert_not_called()

    @pytest.mark.asyncio
    async def test_line_plugin_absent_degrades(self):
        """If WhitelistStore import fails, an approve tap must not crash."""
        adapter = _make_adapter()
        update, query = _make_query("linewl:approve:group:Cabc123")
        with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "*"}, clear=False):
            with patch(
                "plugins.platforms.line.whitelist_store.WhitelistStore",
                side_effect=ImportError("line plugin missing"),
            ):
                await adapter._handle_callback_query(update, MagicMock())
        # degrades: answered with "unavailable", never edits
        query.answer.assert_called_once()
        query.edit_message_text.assert_not_called()
