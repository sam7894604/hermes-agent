"""Tests for Telegram auto-choice buttons (? / ?? prefix mechanism).

Mirrors test_discord_auto_choice_buttons.py structure.  Tests are intentionally
light: we verify the adapter wires up correctly without trying to re-test the
shared detection logic exhaustively (that lives in test_auto_choice_utils.py or
the Discord suite).

Coverage:
  TestDetectInlineChoicesShared – sanity-check shared detection from utils module
  TestFeatureFlag               – _telegram_auto_choice_buttons() honours config
  TestMaybeSendChoiceButtonsTg  – button follow-up is sent / skipped correctly
  TestCallbackAcRouting         – _handle_callback_query routes ac: callbacks
  TestInjectUserChoiceTg        – _inject_user_choice_tg dispatches MessageEvent
"""
from __future__ import annotations

import os
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from gateway.config import PlatformConfig
from plugins.platforms.telegram.adapter import TelegramAdapter


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_adapter(extra: dict | None = None) -> TelegramAdapter:
    """Return a TelegramAdapter with a mocked bot (no real network calls)."""
    config = PlatformConfig(enabled=True, token="test-token", extra=extra or {})
    adapter = TelegramAdapter(config)
    adapter._bot = AsyncMock()
    adapter._app = MagicMock()
    return adapter


def _make_query(
    data: str,
    user_id: int = 99,
    user_name: str = "TestUser",
    chat_id: int = 12345,
    chat_type: str = "private",
    thread_id: int | None = None,
) -> AsyncMock:
    """Build a minimal mock CallbackQuery matching the real object shape."""
    query = AsyncMock()
    query.data = data

    from_user = MagicMock()
    from_user.id = user_id
    from_user.first_name = user_name
    from_user.full_name = user_name
    query.from_user = from_user

    chat = MagicMock()
    chat.id = chat_id
    # chat.type can be a plain string or an enum with .value; adapter handles both
    chat.type = chat_type

    msg = MagicMock()
    msg.chat = chat
    msg.chat_id = chat_id
    msg.text = "Choose one:"
    msg.message_thread_id = thread_id
    query.message = msg

    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    query.edit_message_reply_markup = AsyncMock()
    return query


# ---------------------------------------------------------------------------
# TestDetectInlineChoicesShared
# ---------------------------------------------------------------------------

class TestDetectInlineChoicesShared:
    """Sanity-checks that the shared detection util is importable and correct."""

    def test_numbered_list_with_prefix(self):
        from gateway.platforms.auto_choice_utils import _detect_inline_choices
        content = "? Pick one:\n1. Alpha\n2. Beta\n3. Gamma"
        choices, multi = _detect_inline_choices(content)
        assert choices == ["Alpha", "Beta", "Gamma"]
        assert multi is False

    def test_no_prefix_returns_empty(self):
        from gateway.platforms.auto_choice_utils import _detect_inline_choices
        content = "Here are some items:\n1. Alpha\n2. Beta"
        choices, multi = _detect_inline_choices(content)
        assert choices == []

    def test_double_prefix_marks_multi(self):
        from gateway.platforms.auto_choice_utils import _detect_inline_choices
        content = "?? Choose all that apply:\n- Red\n- Green\n- Blue"
        choices, multi = _detect_inline_choices(content)
        assert len(choices) >= 2
        assert multi is True

    def test_fewer_than_two_choices_returns_empty(self):
        from gateway.platforms.auto_choice_utils import _detect_inline_choices
        content = "? Just one:\n1. Only"
        choices, multi = _detect_inline_choices(content)
        assert choices == []


# ---------------------------------------------------------------------------
# TestFeatureFlag
# ---------------------------------------------------------------------------

class TestFeatureFlag:
    def test_default_enabled(self):
        adapter = _make_adapter()
        assert adapter._telegram_auto_choice_buttons() is True

    def test_disabled_via_extra(self):
        adapter = _make_adapter(extra={"auto_choice_buttons": False})
        assert adapter._telegram_auto_choice_buttons() is False

    def test_enabled_explicit_true(self):
        adapter = _make_adapter(extra={"auto_choice_buttons": True})
        assert adapter._telegram_auto_choice_buttons() is True


# ---------------------------------------------------------------------------
# TestMaybeSendChoiceButtonsTg
# ---------------------------------------------------------------------------

class TestMaybeSendChoiceButtonsTg:
    """Tests for the _maybe_send_choice_buttons_tg() hook."""

    @pytest.mark.asyncio
    async def test_sends_buttons_for_choice_content(self):
        adapter = _make_adapter()
        content = "? Which colour?\n1. Red\n2. Green\n3. Blue"

        mock_msg = MagicMock()
        mock_msg.message_id = 999

        with patch.object(
            adapter, "_send_message_with_thread_fallback", new_callable=AsyncMock
        ) as mock_send:
            mock_send.return_value = mock_msg
            await adapter._maybe_send_choice_buttons_tg(
                chat_id="12345",
                content=content,
                message_id="100",
                metadata=None,
            )

        mock_send.assert_awaited_once()
        # State should be populated
        assert len(adapter._auto_choice_state) == 1
        ac_id = next(iter(adapter._auto_choice_state))
        choices, _ms, chat_id, _tid, btn_msg_id = adapter._auto_choice_state[ac_id]
        assert choices == ["Red", "Green", "Blue"]
        assert chat_id == "12345"
        assert btn_msg_id == "999"

    @pytest.mark.asyncio
    async def test_skips_when_no_choices(self):
        adapter = _make_adapter()
        content = "Here is some information without a choice prompt."

        with patch.object(
            adapter, "_send_message_with_thread_fallback", new_callable=AsyncMock
        ) as mock_send:
            await adapter._maybe_send_choice_buttons_tg(
                chat_id="12345",
                content=content,
                message_id="100",
                metadata=None,
            )

        mock_send.assert_not_awaited()
        assert adapter._auto_choice_state == {}

    @pytest.mark.asyncio
    async def test_skips_when_feature_disabled(self):
        adapter = _make_adapter(extra={"auto_choice_buttons": False})
        content = "? Pick one:\n1. Alpha\n2. Beta"

        with patch.object(
            adapter, "_send_message_with_thread_fallback", new_callable=AsyncMock
        ) as mock_send:
            await adapter._maybe_send_choice_buttons_tg(
                chat_id="12345",
                content=content,
                message_id="100",
                metadata=None,
            )

        mock_send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_skips_when_no_bot(self):
        adapter = _make_adapter()
        adapter._bot = None  # simulate uninitialised bot
        content = "? Pick one:\n1. Alpha\n2. Beta"

        with patch.object(
            adapter, "_send_message_with_thread_fallback", new_callable=AsyncMock
        ) as mock_send:
            await adapter._maybe_send_choice_buttons_tg(
                chat_id="12345",
                content=content,
                message_id="100",
                metadata=None,
            )

        mock_send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_swallows_send_exception(self):
        """Network errors inside _maybe_send_choice_buttons_tg must not propagate."""
        adapter = _make_adapter()
        content = "? Which colour?\n1. Red\n2. Green"

        with patch.object(
            adapter, "_send_message_with_thread_fallback", new_callable=AsyncMock
        ) as mock_send:
            mock_send.side_effect = RuntimeError("network error")
            # Should not raise
            await adapter._maybe_send_choice_buttons_tg(
                chat_id="12345",
                content=content,
                message_id="100",
                metadata=None,
            )

        # State must stay empty (exception was swallowed before state was written)
        assert adapter._auto_choice_state == {}

    @pytest.mark.asyncio
    async def test_monotonic_counter_increments(self):
        """Each call gets a distinct ac_id."""
        adapter = _make_adapter()
        content = "? Pick:\n1. A\n2. B"
        mock_msg = MagicMock()
        mock_msg.message_id = 1

        with patch.object(
            adapter, "_send_message_with_thread_fallback", new_callable=AsyncMock
        ) as mock_send:
            mock_send.return_value = mock_msg
            await adapter._maybe_send_choice_buttons_tg("1", content, "10", None)
            await adapter._maybe_send_choice_buttons_tg("1", content, "11", None)

        assert len(adapter._auto_choice_state) == 2
        ids = list(adapter._auto_choice_state.keys())
        assert ids[0] != ids[1]


# ---------------------------------------------------------------------------
# TestCallbackAcRouting
# ---------------------------------------------------------------------------

class TestCallbackAcRouting:
    """Tests for the ac: handling block inside _handle_callback_query."""

    def _seed_state(self, adapter: TelegramAdapter, ac_id: str) -> None:
        """Pre-populate _auto_choice_state with a 5-tuple matching production."""
        adapter._auto_choice_state[ac_id] = (
            ["Red", "Green", "Blue"],  # choices
            False,                     # multi_select
            "12345",                   # chat_id
            None,                      # thread_id
            "999",                     # btn_msg_id
        )

    @pytest.mark.asyncio
    async def test_choice_click_injects_user_turn(self, monkeypatch):
        adapter = _make_adapter()
        self._seed_state(adapter, "7")

        injected: list[str] = []

        async def _fake_inject(query, choice_text):
            injected.append(choice_text)

        monkeypatch.setattr(adapter, "_inject_user_choice_tg", _fake_inject)
        monkeypatch.setenv("GATEWAY_ALLOW_ALL_USERS", "true")

        query = _make_query("ac:7:1", user_id=42)  # idx=1 → "Green"
        update = MagicMock()
        update.callback_query = query
        context = MagicMock()

        await adapter._handle_callback_query(update, context)

        assert injected == ["Green"]
        # State must be popped (single-use)
        assert "7" not in adapter._auto_choice_state
        query.answer.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_other_dismisses_buttons(self, monkeypatch):
        adapter = _make_adapter()
        self._seed_state(adapter, "8")
        monkeypatch.setenv("GATEWAY_ALLOW_ALL_USERS", "true")

        injected: list[str] = []

        async def _fake_inject(query, choice_text):  # pragma: no cover
            injected.append(choice_text)

        monkeypatch.setattr(adapter, "_inject_user_choice_tg", _fake_inject)

        query = _make_query("ac:8:other", user_id=42)
        update = MagicMock()
        update.callback_query = query
        context = MagicMock()

        await adapter._handle_callback_query(update, context)

        assert injected == []
        assert "8" not in adapter._auto_choice_state
        query.answer.assert_awaited_once()
        query.edit_message_reply_markup.assert_awaited_once_with(reply_markup=None)

    @pytest.mark.asyncio
    async def test_unauthorized_user_rejected(self, monkeypatch):
        adapter = _make_adapter()
        self._seed_state(adapter, "9")
        # No TELEGRAM_ALLOWED_USERS → deny (fail-closed)
        monkeypatch.delenv("TELEGRAM_ALLOWED_USERS", raising=False)
        monkeypatch.delenv("GATEWAY_ALLOW_ALL_USERS", raising=False)

        injected: list[str] = []

        async def _fake_inject(query, choice_text):  # pragma: no cover
            injected.append(choice_text)

        monkeypatch.setattr(adapter, "_inject_user_choice_tg", _fake_inject)

        query = _make_query("ac:9:0", user_id=999)
        update = MagicMock()
        update.callback_query = query
        context = MagicMock()

        await adapter._handle_callback_query(update, context)

        assert injected == []
        answer_call = query.answer.await_args
        assert "not authorized" in str(answer_call).lower()
        # State must NOT be consumed
        assert "9" in adapter._auto_choice_state

    @pytest.mark.asyncio
    async def test_already_resolved_noop(self, monkeypatch):
        adapter = _make_adapter()
        # No state for ac_id "42"
        monkeypatch.setenv("GATEWAY_ALLOW_ALL_USERS", "true")

        injected: list[str] = []

        async def _fake_inject(query, choice_text):  # pragma: no cover
            injected.append(choice_text)

        monkeypatch.setattr(adapter, "_inject_user_choice_tg", _fake_inject)

        query = _make_query("ac:42:0", user_id=1)
        update = MagicMock()
        update.callback_query = query
        context = MagicMock()

        await adapter._handle_callback_query(update, context)

        assert injected == []
        answer_call = query.answer.await_args
        assert "already been resolved" in str(answer_call).lower()


# ---------------------------------------------------------------------------
# TestInjectUserChoiceTg
# ---------------------------------------------------------------------------

class TestInjectUserChoiceTg:
    """Tests for _inject_user_choice_tg() building and dispatching MessageEvent."""

    @pytest.mark.asyncio
    async def test_dispatches_handle_message_with_choice_text(self):
        adapter = _make_adapter()

        dispatched: list = []

        async def _fake_handle(event):
            dispatched.append(event)

        adapter.handle_message = _fake_handle

        query = _make_query("ac:1:0", user_id=55, chat_id=67890, chat_type="private")
        await adapter._inject_user_choice_tg(query, "Red")

        assert len(dispatched) == 1
        event = dispatched[0]
        from gateway.platforms.base import MessageEvent, MessageType
        assert isinstance(event, MessageEvent)
        assert event.text == "Red"
        assert event.message_type == MessageType.TEXT
        assert event.raw_message is None  # synthetic event

    @pytest.mark.asyncio
    async def test_returns_early_when_no_message(self):
        adapter = _make_adapter()

        dispatched: list = []

        async def _fake_handle(event):  # pragma: no cover
            dispatched.append(event)

        adapter.handle_message = _fake_handle

        query = AsyncMock()
        query.message = None
        await adapter._inject_user_choice_tg(query, "Red")

        assert dispatched == []

    @pytest.mark.asyncio
    async def test_builds_dm_source_for_private_chat(self):
        adapter = _make_adapter()
        sources: list = []

        async def _fake_handle(event):
            sources.append(event.source)

        adapter.handle_message = _fake_handle

        query = _make_query("ac:1:0", user_id=55, chat_id=67890, chat_type="private")
        await adapter._inject_user_choice_tg(query, "Alpha")

        assert len(sources) == 1
        # chat_type should be normalised to "dm" for private chats
        assert sources[0].chat_type == "dm"
        assert sources[0].user_id == "55"
