"""Phase-4 adapter integration tests for the LINE whitelist subsystem.

Covers the routing policy wired into ``_handle_message_event``:
- @mention parsing (`_bot_mentioned`)
- env-overlay + store authorization (`_source_authorized`)
- unauthorized DM / group reject paths (English reply + notify + record)
- authorized group observe-record vs @mention trigger
- passive-media policy (video dropped) and quote-reply context injection.

The adapter is loaded via the shared plugin loader so it doesn't collide
with sibling platform-plugin tests under xdist.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.gateway._plugin_adapter_loader import load_plugin_adapter

_line = load_plugin_adapter("line")

LineAdapter = _line.LineAdapter
_bot_mentioned = _line._bot_mentioned
_message_text = _line._message_text
UNAUTH_DM_REPLY = _line.UNAUTH_DM_REPLY
UNAUTH_GROUP_REPLY = _line.UNAUTH_GROUP_REPLY


def _make_adapter():
    from gateway.config import PlatformConfig
    ad = LineAdapter(PlatformConfig(enabled=True, extra={
        "channel_access_token": "tok",
        "channel_secret": "sec",
    }))
    ad._bot_user_id = "Ubot"
    ad._client = None  # name resolution falls back to raw ids
    ad.handle_message = AsyncMock()
    # Fresh mock store per adapter — routing tests set behaviors explicitly.
    ad._whitelist = MagicMock()
    ad._whitelist.requires_mention.return_value = True
    ad._whitelist.should_reply_unauthorized.return_value = True
    return ad


def _msg_event(source, message):
    return {"type": "message", "source": source, "replyToken": "rt", "message": message}


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

class TestMentionParsing:
    def test_is_self(self):
        msg = {"type": "text", "text": "hi", "mention": {"mentionees": [{"isSelf": True}]}}
        assert _bot_mentioned(msg, "Ubot")

    def test_user_id_match(self):
        msg = {"type": "text", "mention": {"mentionees": [{"userId": "Ubot"}]}}
        assert _bot_mentioned(msg, "Ubot")

    def test_type_all_ignored(self):
        msg = {"type": "text", "mention": {"mentionees": [{"type": "all"}]}}
        assert not _bot_mentioned(msg, "Ubot")

    def test_no_mention(self):
        assert not _bot_mentioned({"type": "text", "text": "hi"}, "Ubot")

    def test_other_user_mention(self):
        msg = {"mention": {"mentionees": [{"userId": "Uother"}]}}
        assert not _bot_mentioned(msg, "Ubot")


def test_message_text():
    assert _message_text({"type": "text", "text": "hello"}) == "hello"
    assert _message_text({"type": "image"}) == "[image]"


# ---------------------------------------------------------------------------
# _source_authorized — env overlay OR live store
# ---------------------------------------------------------------------------

class TestSourceAuthorized:
    def test_env_overlay_group(self):
        ad = _make_adapter()
        ad.allow_all = False
        ad.allowed_groups = {"Cok"}
        ad._whitelist.is_allowed.return_value = False
        assert ad._source_authorized({"type": "group", "groupId": "Cok"})

    def test_store_authorizes(self):
        ad = _make_adapter()
        ad.allow_all = False
        ad.allowed_groups = set()
        ad._whitelist.is_allowed.return_value = True
        assert ad._source_authorized({"type": "group", "groupId": "Cnew"})
        ad._whitelist.is_allowed.assert_called_with("group", "Cnew")

    def test_denied(self):
        ad = _make_adapter()
        ad.allow_all = False
        ad.allowed_users = ad.allowed_groups = ad.allowed_rooms = set()
        ad._whitelist.is_allowed.return_value = False
        assert not ad._source_authorized({"type": "user", "userId": "Ux"})


# ---------------------------------------------------------------------------
# Routing policy
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestRouting:
    async def test_unauthorized_dm_rejects_and_records(self):
        ad = _make_adapter()
        ad._send_plain = AsyncMock()
        ad._notify_admin_unauthorized = AsyncMock()
        src = {"type": "user", "userId": "Ustranger"}
        await ad._handle_message_event(
            _msg_event(src, {"type": "text", "id": "m1", "text": "hi"}),
            authorized=False,
        )
        ad.handle_message.assert_not_called()
        ad._send_plain.assert_awaited_once()
        assert ad._send_plain.await_args.args[2] == UNAUTH_DM_REPLY
        ad._notify_admin_unauthorized.assert_awaited_once()
        # attempt logged into the pending queue as a DM source
        ad._whitelist.record_attempt.assert_called_once()
        assert ad._whitelist.record_attempt.call_args.kwargs["source_type"] == "dm"

    async def test_unauthorized_group_records_pending(self):
        ad = _make_adapter()
        ad._send_plain = AsyncMock()
        ad._notify_admin_unauthorized = AsyncMock()
        src = {"type": "group", "groupId": "Cx", "userId": "Uy"}
        msg = {"type": "text", "id": "m1", "text": "@bot hi",
               "mention": {"mentionees": [{"isSelf": True}]}}
        await ad._handle_message_event(_msg_event(src, msg), authorized=False)
        # group attempt logged into the pending queue (source_type=group)
        ad._whitelist.record_attempt.assert_called_once()
        kw = ad._whitelist.record_attempt.call_args.kwargs
        assert kw["source_type"] == "group"
        assert ad._whitelist.record_attempt.call_args.args[0] == "Cx"

    async def test_unauthorized_group_mention_replies(self):
        ad = _make_adapter()
        ad._send_plain = AsyncMock()
        ad._notify_admin_unauthorized = AsyncMock()
        src = {"type": "group", "groupId": "Cx", "userId": "Uy"}
        msg = {"type": "text", "id": "m1", "text": "@bot hi",
               "mention": {"mentionees": [{"isSelf": True}]}}
        await ad._handle_message_event(_msg_event(src, msg), authorized=False)
        ad.handle_message.assert_not_called()
        ad._send_plain.assert_awaited_once()
        assert ad._send_plain.await_args.args[2] == UNAUTH_GROUP_REPLY

    async def test_unauthorized_group_no_mention_silent(self):
        ad = _make_adapter()
        ad._send_plain = AsyncMock()
        ad._notify_admin_unauthorized = AsyncMock()
        ad._observe_record = AsyncMock()
        src = {"type": "group", "groupId": "Cx", "userId": "Uy"}
        await ad._handle_message_event(
            _msg_event(src, {"type": "text", "id": "m1", "text": "chatter"}),
            authorized=False,
        )
        ad.handle_message.assert_not_called()
        ad._send_plain.assert_not_awaited()
        ad._observe_record.assert_not_awaited()  # non-whitelisted group: no observe

    async def test_authorized_group_no_mention_observes(self):
        ad = _make_adapter()
        ad._observe_record = AsyncMock()
        src = {"type": "group", "groupId": "Cok", "userId": "Uy"}
        await ad._handle_message_event(
            _msg_event(src, {"type": "text", "id": "m1", "text": "chatter"}),
            authorized=True,
        )
        ad.handle_message.assert_not_called()
        ad._observe_record.assert_awaited_once()

    async def test_authorized_group_no_mention_failopen_when_bot_id_unknown(self):
        # SAFETY: if get_bot_user_id() failed at connect (_bot_user_id is None),
        # _bot_mentioned can never be True. The mention gate must FAIL-OPEN —
        # trigger the agent (pre-whitelist behaviour) rather than silence the
        # whole authorized group — and warn exactly once.
        ad = _make_adapter()
        ad._bot_user_id = None  # simulate GET /v2/bot/info failure
        ad._observe_record = AsyncMock()
        src = {"type": "group", "groupId": "Cok", "userId": "Uy"}
        await ad._handle_message_event(
            _msg_event(src, {"type": "text", "id": "m1", "text": "no at, bot id unknown"}),
            authorized=True,
        )
        ad._observe_record.assert_not_awaited()   # NOT silenced
        ad.handle_message.assert_awaited_once()    # triggered (fail-open)
        assert ad._mention_gate_warned is True     # warned
        # second message must not re-warn (once-per-connection)
        ad.handle_message.reset_mock()
        await ad._handle_message_event(
            _msg_event(src, {"type": "text", "id": "m2", "text": "again"}),
            authorized=True,
        )
        ad.handle_message.assert_awaited_once()

    async def test_authorized_group_mention_triggers(self):
        ad = _make_adapter()
        src = {"type": "group", "groupId": "Cok", "userId": "Uy"}
        msg = {"type": "text", "id": "m1", "text": "@bot go",
               "mention": {"mentionees": [{"isSelf": True}]}}
        await ad._handle_message_event(_msg_event(src, msg), authorized=True)
        ad.handle_message.assert_awaited_once()

    async def test_authorized_dm_triggers_without_mention(self):
        ad = _make_adapter()
        src = {"type": "user", "userId": "Uok"}
        await ad._handle_message_event(
            _msg_event(src, {"type": "text", "id": "m1", "text": "hi"}),
            authorized=True,
        )
        ad.handle_message.assert_awaited_once()

    async def test_requires_mention_off_triggers(self):
        ad = _make_adapter()
        ad._whitelist.requires_mention.return_value = False
        src = {"type": "group", "groupId": "Cok", "userId": "Uy"}
        await ad._handle_message_event(
            _msg_event(src, {"type": "text", "id": "m1", "text": "no at needed"}),
            authorized=True,
        )
        ad.handle_message.assert_awaited_once()


# ---------------------------------------------------------------------------
# Observe media policy + quote reply
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestObserveAndQuote:
    def _store_with_session(self, messages=None):
        store = MagicMock()
        store.get_or_create_session.return_value = MagicMock(session_id="sid")
        db = MagicMock()
        db.get_messages.return_value = messages or []
        store._db = db
        store.append_to_transcript = MagicMock()
        return store

    async def test_video_dropped_from_observe(self):
        ad = _make_adapter()
        ad._session_store = self._store_with_session()
        await ad._observe_record(
            source={"type": "group", "groupId": "Cok", "userId": "Uy"},
            chat_id="Cok", chat_type="group", user_id="Uy",
            msg={"type": "video", "id": "m1"}, msg_type="video", message_id="m1",
        )
        ad._session_store.append_to_transcript.assert_not_called()

    async def test_text_observed_with_attribution(self):
        ad = _make_adapter()
        ad._session_store = self._store_with_session()
        await ad._observe_record(
            source={"type": "group", "groupId": "Cok", "userId": "Uy"},
            chat_id="Cok", chat_type="group", user_id="Uy",
            msg={"type": "text", "id": "m2", "text": "spent $30"}, msg_type="text",
            message_id="m2",
        )
        ad._session_store.append_to_transcript.assert_called_once()
        _sid, record = ad._session_store.append_to_transcript.call_args.args
        assert record["observed"] is True
        assert record["platform_message_id"] == "m2"
        assert record["content"] == "[Uy|Uy]\nspent $30"

    async def test_quote_context_injected(self):
        ad = _make_adapter()
        ad._session_store = self._store_with_session(
            messages=[{"platform_message_id": "orig", "content": "[Alice|Ua]\nreceipt $12"}]
        )
        src = {"type": "group", "groupId": "Cok", "userId": "Uy"}
        msg = {"type": "text", "id": "m3", "text": "@bot log this",
               "quotedMessageId": "orig",
               "mention": {"mentionees": [{"isSelf": True}]}}
        await ad._handle_message_event(_msg_event(src, msg), authorized=True)
        ad.handle_message.assert_awaited_once()
        event_obj = ad.handle_message.await_args.args[0]
        assert "receipt $12" in event_obj.text
        assert "log this" in event_obj.text

    async def test_quote_missing_degrades(self):
        ad = _make_adapter()
        ad._session_store = self._store_with_session(messages=[])
        src = {"type": "user", "userId": "Uok"}
        msg = {"type": "text", "id": "m4", "text": "reply", "quotedMessageId": "gone"}
        await ad._handle_message_event(_msg_event(src, msg), authorized=True)
        ad.handle_message.assert_awaited_once()
        event_obj = ad.handle_message.await_args.args[0]
        assert "earlier message" in event_obj.text.lower()
