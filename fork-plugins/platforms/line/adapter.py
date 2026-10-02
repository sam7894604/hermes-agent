"""LINE platform override — this fork's whitelist / passive-context / media-backfill layer.

Installed at ``$HERMES_HOME/plugins/platforms/line/`` it shadows the bundled ``platforms/line`` plugin
(``hermes_cli/plugins_discovery.py::resolve_manifest_winners``). It does NOT copy the stock adapter:
``LineAdapter`` and ``_LineClient`` subclass upstream's ``plugins.platforms.line.adapter`` and override
only the event-routing seams, so upstream fixes to the webhook server, media, postback and send paths
keep flowing in. ``register()`` re-runs upstream's own registration through a delegating context,
swapping just the adapter factory, so the platform entry (label, env names, limits, hint) stays
upstream's.

What the override adds, on top of the static ``LINE_ALLOWED_*`` env allowlists:

* a hot-reloading, config.yaml-backed whitelist (``whitelist_store.py``) with a pending queue, admin
  notification (``whitelist_notify.py``; Telegram/Discord get an interactive card from their own
  override plugins) and throttled English rejections for strangers;
* ``requires_mention`` gating in groups, with quote-reply of the bot's own message counting as a
  mention, and a fail-open when the bot's own userId could not be resolved at connect;
* passive observe-recording of unmentioned group messages (shared chat-scoped session) that is fed
  back as ``channel_context``, plus on-demand backfill of recently uploaded images/files (download-once
  and vision-once caches keyed by LINE message id);
* display-name resolution (profile / group summary / member endpoints) with a TTL cache;
* Markdown tables converted to bullet groups before the stock Markdown stripping (LINE has no table
  syntax; same shared converter Discord/Telegram use);
* the ``line_whitelist`` agent tool (``line_whitelist_tool.py``), registered under the same name.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections import OrderedDict, deque
from typing import Any, Deque, Dict, List, Optional, Tuple

from gateway.platforms.event import MessageEvent, MessageType
from gateway.platforms.helpers import convert_table_to_bullets
from plugins.platforms.line import adapter as _base

from .whitelist_store import WhitelistStore

logger = logging.getLogger(__name__)

# Name resolution (display names for the dashboard / observed context)
LINE_PROFILE_URL_FMT = "https://api.line.me/v2/bot/profile/{user_id}"
LINE_GROUP_SUMMARY_URL_FMT = "https://api.line.me/v2/bot/group/{group_id}/summary"
LINE_GROUP_MEMBER_URL_FMT = "https://api.line.me/v2/bot/group/{group_id}/member/{user_id}"
LINE_ROOM_MEMBER_URL_FMT = "https://api.line.me/v2/bot/room/{room_id}/member/{user_id}"

# Unauthorized-source English replies.
UNAUTH_GROUP_REPLY = (
    "This group isn't authorized to use the assistant yet. "
    "An administrator has been notified — please wait for approval."
)
UNAUTH_DM_REPLY = (
    "You're not authorized to use this assistant yet. "
    "An administrator has been notified — please contact the admin for access."
)


def strip_markdown_preserving_urls(text: str) -> str:
    """Upstream's Markdown stripping, with GFM pipe tables turned into bullet groups first.

    The converter deliberately skips fenced code blocks, and it must run BEFORE the fences are stripped:
    once they are gone a table inside a code block would look like a real table. It emits ``**heading**``
    + ``• field: value``; the bold markers are stripped by upstream's rules and ``•`` bullets pass
    through untouched (the bullet rule only rewrites ``-``/``*``/``+`` markers).
    """
    if not text:
        return text
    return _base.strip_markdown_preserving_urls(convert_table_to_bullets(text))


def _message_text(msg: Dict[str, Any]) -> str:
    """Best-effort plain-text extraction of an inbound message (for logging)."""
    if (msg or {}).get("type") == "text":
        return msg.get("text", "") or ""
    return f"[{(msg or {}).get('type', 'unknown')}]"


def _bot_mentioned(msg: Dict[str, Any], bot_user_id: Optional[str]) -> bool:
    """True if this text message @-mentions the bot.

    LINE puts mentions at ``message.mention.mentionees[]``; each entry carries ``isSelf`` (bool) and
    ``userId``. A ``type == "all"`` (@everyone) entry is NOT treated as addressing the bot specifically.
    """
    mention = (msg or {}).get("mention") or {}
    for m in mention.get("mentionees", []) or []:
        if not isinstance(m, dict):
            continue
        if m.get("type") == "all":
            continue
        if m.get("isSelf"):
            return True
        if bot_user_id and m.get("userId") == bot_user_id:
            return True
    return False


class _LineClient(_base._LineClient):
    """Upstream's client plus sent-message tracking and the name-resolution GET endpoints."""

    def __init__(self, channel_access_token: str, **kwargs: Any) -> None:
        super().__init__(channel_access_token, **kwargs)
        # Ids of messages this bot has sent (from the reply/push response ``sentMessages``). A quote-reply
        # of one of them counts as an implicit @mention. Bounded so it can't grow without limit.
        self.sent_message_ids: Deque[str] = deque(maxlen=500)

    def _record_sent(self, payload: Any) -> None:
        try:
            for m in (payload or {}).get("sentMessages", []) or []:
                mid = m.get("id")
                if mid:
                    self.sent_message_ids.append(str(mid))
        except Exception:
            pass

    async def _post_messages(self, url: str, label: str, payload: Dict[str, Any]) -> None:
        # Mirrors upstream's body; the only addition is reading ``sentMessages`` off the response.
        async with self._session(self._timeout) as session:
            async with session.post(url, headers=self._headers, json=payload) as resp:
                if resp.status >= 400:
                    body = await resp.text()
                    raise RuntimeError(f"LINE {label} {resp.status}: {body[:200]}")
                try:
                    self._record_sent(await resp.json())
                except Exception:
                    pass

    async def _get_json_field(self, url: str, field: str) -> Optional[str]:
        """Shared GET helper for name-resolution endpoints. Best-effort."""
        try:
            async with self._session(10.0) as session:
                async with session.get(url, headers=self._headers) as resp:
                    if resp.status >= 400:
                        return None
                    data = await resp.json()
                    return data.get(field)
        except Exception:
            return None

    async def get_profile(self, user_id: str) -> Optional[str]:
        """Display name for a 1:1 / followed user (``GET /v2/bot/profile/{id}``)."""
        if not user_id:
            return None
        return await self._get_json_field(LINE_PROFILE_URL_FMT.format(user_id=user_id), "displayName")

    async def get_group_summary(self, group_id: str) -> Optional[str]:
        """Group name (``GET /v2/bot/group/{id}/summary``)."""
        if not group_id:
            return None
        return await self._get_json_field(LINE_GROUP_SUMMARY_URL_FMT.format(group_id=group_id), "groupName")

    async def get_member_name(self, chat_id: str, user_id: str, *, chat_type: str = "group") -> Optional[str]:
        """Display name of a member inside a group/room (the plain ``/profile`` endpoint does not work for
        arbitrary group members)."""
        if not chat_id or not user_id:
            return None
        if chat_type == "room":
            url = LINE_ROOM_MEMBER_URL_FMT.format(room_id=chat_id, user_id=user_id)
        else:
            url = LINE_GROUP_MEMBER_URL_FMT.format(group_id=chat_id, user_id=user_id)
        return await self._get_json_field(url, "displayName")


class LineAdapter(_base.LineAdapter):
    """Upstream's adapter with the whitelist routing policy layered over ``_dispatch_event``."""

    def __init__(self, config, **kwargs):
        super().__init__(config, **kwargs)
        extra = getattr(config, "extra", {}) or {}
        # Hot-reload store backed by config.yaml (platforms.line.*). The static env allowlists stay as a
        # backward-compatible overlay: a source is authorized if it matches EITHER the env sets OR the live
        # store, so env-only deployments keep working while new entries hot-reload.
        try:
            self._whitelist: Optional[WhitelistStore] = WhitelistStore()
        except Exception as exc:
            logger.warning("LINE: WhitelistStore init failed (%s); env allowlists only", exc)
            self._whitelist = None
        # Passive-context (observed) group recording. Default on; ``observe_unmentioned`` disables.
        self._observe_unmentioned = _base._truthy_env(
            "LINE_OBSERVE_UNMENTIONED", bool(extra.get("observe_unmentioned", True)))
        # On-demand media backfill: observed uploads are recorded as a lightweight placeholder ONLY; when
        # the bot IS later triggered and the triggering message carries no media, recently observed
        # image/file uploads inside this window are pulled into the turn. Env default here; the live
        # value is the dashboard-editable ``media_backfill_window_minutes`` (see the store).
        self._backfill_window_minutes_default = float(
            os.getenv("LINE_MEDIA_BACKFILL_WINDOW_MIN") or extra.get("media_backfill_window_minutes", 1))
        # The same upload can be pulled into several trigger turns inside the window, so both the LINE
        # download and the vision extraction are memoized by the (immutable) LINE message id. Bounded FIFO.
        self._bf_download_cache: "OrderedDict[str, Tuple[str, str]]" = OrderedDict()
        self._bf_vision_cache: "OrderedDict[str, str]" = OrderedDict()
        self._bf_cache_max = int(
            os.getenv("LINE_MEDIA_BACKFILL_CACHE_MAX") or extra.get("media_backfill_cache_max", 256))
        # Name-resolution TTL cache: id -> (display_name, expiry_ts).
        self._name_cache: Dict[str, Tuple[str, float]] = {}
        self._name_cache_ttl = float(os.getenv("LINE_NAME_CACHE_TTL") or extra.get("name_cache_ttl", 3600))
        # One-shot flag so the fail-open mention-gate warning is logged once per connection.
        self._mention_gate_warned: bool = False

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        self._mention_gate_warned = False  # fresh warning per connection cycle
        ok = await super().connect(is_reconnect=is_reconnect)
        if not ok:
            return ok
        # Upstream built a stock client; swap in ours (same token) so sent-message ids are tracked.
        if self._client is not None and not isinstance(self._client, _LineClient):
            self._client = _LineClient(self.channel_access_token)
        if self._bot_user_id:
            logger.info("LINE: bot userId resolved (%s…) — @mention gate active", self._bot_user_id[:8])
        else:
            logger.warning(
                "LINE: bot userId NOT resolved at connect — @mention gate will fail-open "
                "(authorized groups reply without @mention).")
        return ok

    def format_message(self, content: str) -> str:
        """Strip Markdown that LINE can't render (tables → bullets first). URLs are preserved."""
        return strip_markdown_preserving_urls(content)

    # ------------------------------------------------------------------
    # Event routing (replaces upstream's static-allowlist gate)
    # ------------------------------------------------------------------

    async def _dispatch_event(self, event: Dict[str, Any]) -> None:
        event_type = event.get("type")
        source = event.get("source") or {}
        webhook_event_id = event.get("webhookEventId", "") or ""
        if webhook_event_id and self._dedup.is_duplicate(webhook_event_id):  # at-least-once redelivery
            logger.debug("LINE: ignoring duplicate webhook event %s", webhook_event_id)
            return
        if self._bot_user_id and source.get("userId", "") == self._bot_user_id:
            return

        authorized = self._source_authorized(source)

        if event_type == "message":
            # Message events carry the routing nuance (@mention gating, unauthorized English reply, passive
            # observe-recording), so the authorization *decision* is made here but the *policy* is applied
            # inside the handler where replyToken / mention / text are known.
            await self._handle_message_event(event, authorized=authorized)
        elif event_type == "postback":
            # Postbacks only make sense from an already-authorized source (the button was sent to them
            # after a prior authorized turn).
            if not authorized:
                logger.info("LINE: rejecting postback from unauthorized %s", source)
                return
            await self._handle_postback_event(event)
        elif event_type in {"follow", "join"}:
            # New source reached the bot — notify admins once (dedup) so they can approve it.
            logger.info("LINE: lifecycle event %s from %s", event_type, source)
            if not authorized:
                await self._maybe_notify_new_source(source)
        elif event_type in {"unfollow", "leave"}:
            logger.info("LINE: lifecycle event %s from %s", event_type, source)
        else:
            logger.debug("LINE: ignoring event type %r", event_type)

    async def _handle_message_event(self, event: Dict[str, Any], *, authorized: bool = True) -> None:
        msg = event.get("message") or {}
        msg_type, message_id = msg.get("type", ""), msg.get("id", "")
        reply_token = event.get("replyToken", "")
        source = event.get("source") or {}
        chat_id, chat_type = _base._resolve_chat(source)
        user_id = source.get("userId", "") or chat_id

        # Stash the reply token for outbound use (reject replies use it too).
        if chat_id and reply_token:
            self._reply_tokens[chat_id] = (reply_token, time.time() + _base.LINE_REPLY_TOKEN_TTL_SECONDS)

        mentioned = _bot_mentioned(msg, self._bot_user_id)

        # Quote-reply of the bot's OWN message = implicit @mention: replying to something the bot said is
        # clearly addressing it. Only an id WE sent counts — quoting another member's message does not.
        if not mentioned:
            qmid = (msg or {}).get("quotedMessageId")
            if qmid and self._client and str(qmid) in getattr(self._client, "sent_message_ids", ()):
                mentioned = True

        # ---- Authorization / routing policy ---------------------------------
        if chat_type == "dm":
            if not authorized:
                # Stranger DM: throttled English reply + notify admins (dedup) + record the attempt. Never
                # trigger the agent.
                await self._reject_unauthorized(
                    chat_id=chat_id, chat_type=chat_type, user_id=user_id, reply_token=reply_token,
                    attempt_text=_message_text(msg), dm=True)
                return
            # authorized DM → trigger (no @mention required)
        else:  # group / room
            if not authorized:
                # Unauthorized group: reply English only if the bot was @'d (throttled) + notify admins
                # (dedup). Non-@ stays silent, and content from a non-whitelisted group is never recorded.
                if mentioned:
                    await self._reject_unauthorized(
                        chat_id=chat_id, chat_type=chat_type, user_id=user_id, reply_token=reply_token,
                        attempt_text="", dm=False)
                return
            requires_mention = self._group_requires_mention(chat_id)
            if requires_mention and self._bot_user_id is None:
                # FAIL-OPEN: matching LINE mentionees needs our own bot userId (GET /v2/bot/info at connect).
                # Without it ``_bot_mentioned`` can never be True, so enforcing the gate would silence every
                # message in an authorized group. Fall back to triggering and warn once.
                self._warn_mention_gate_unavailable()
            elif requires_mention and not mentioned:
                # Passive observe-record — record as context, do NOT trigger.
                await self._observe_record(
                    source=source, chat_id=chat_id, chat_type=chat_type, user_id=user_id, msg=msg,
                    msg_type=msg_type, message_id=message_id)
                return
            # mentioned / requires_mention disabled / gate unavailable → trigger

        # ---- Trigger path: media + build event + handle_message ------------
        media_urls: List[str] = []
        media_types: List[str] = []
        if msg_type == "text":
            text = msg.get("text", "") or ""
        elif msg_type in _base._INBOUND_MEDIA_EXT:  # fetch, cache, surface a vision-friendly local path
            file_name = msg.get("fileName") or msg.get("file_name") or ""
            local_path, media_type = await self._download_media(message_id, msg_type, filename=file_name or None)
            if local_path:
                media_urls.append(local_path)
                media_types.append(media_type)
            # Surface the real filename so the agent can refer to the document naturally.
            text = f"[file: {file_name}]" if (msg_type == "file" and file_name) else f"[{msg_type}]"
        elif msg_type == "sticker":
            text = f"[sticker: {', '.join(msg['keywords'])}]" if msg.get("keywords") else "[sticker]"
        elif msg_type == "location":
            text = f"[location: {msg.get('title', '')} {msg.get('address', '')}]".strip()
        else:
            text = f"[unsupported message type: {msg_type}]"

        # On-demand media backfill: a triggering GROUP message that carries no media of its own (e.g.
        # "@bot what's the amount on that receipt?" after a silent upload) deterministically pulls in the
        # recently OBSERVED uploads inside the backfill window. Images are vision-read (cached) and injected
        # as channel_context text; files are attached as media so the agent can extract them.
        backfill_context: Optional[str] = None
        if not media_urls and chat_type in {"group", "room"}:
            backfill_context, bf_urls, bf_types = await self._backfill_recent_media(chat_id, chat_type)
            if bf_urls:
                media_urls.extend(bf_urls)
                media_types.extend(bf_types)

        if chat_type in {"group", "room"}:
            observed_context = await self._recent_observed_context(chat_id, chat_type)
            backfill_context = "\n\n".join(
                part for part in (observed_context, backfill_context) if part) or None

        # Let the gateway's shared reply-context path render the resolved quote.
        quote_ctx = await self._quote_context(source, chat_id, chat_type, msg)
        quoted_id = str(msg.get("quotedMessageId") or "") or None

        if chat_type == "dm" and self._client:  # best-effort typing indicator (DM only)
            asyncio.create_task(self._client.loading(chat_id))

        # Best-effort display-name resolution (falls back to raw ids).
        user_name = await self._resolve_name(chat_id, chat_type, user_id) or user_id
        chat_name = await self._resolve_chat_name(chat_id, chat_type) or chat_id

        source_obj = self.build_source(
            chat_id=chat_id, chat_type=chat_type, user_id=user_id, user_name=user_name, chat_name=chat_name,
            message_id=message_id)
        await self.handle_message(MessageEvent(
            text=text, message_type=_base._LINE_MESSAGE_TYPES.get(msg_type, MessageType.TEXT), source=source_obj,
            raw_message=event, message_id=message_id, media_urls=media_urls, media_types=media_types,
            reply_to_message_id=quoted_id, reply_to_text=quote_ctx,
            reply_to_is_own_message=bool(
                quoted_id and self._client and quoted_id in getattr(self._client, "sent_message_ids", ())),
            # Deterministic media-backfill content; the gateway prepends it to this turn.
            channel_context=backfill_context))

    # ------------------------------------------------------------------
    # Whitelist — authorization / observe / notify / naming
    # ------------------------------------------------------------------

    def _source_authorized(self, source: Dict[str, Any]) -> bool:
        """Authorized if the source matches the static env allowlists OR the live config-backed store."""
        if _base._allowed_for_source(
                source, allow_all=self.allow_all, user_ids=self.allowed_users, group_ids=self.allowed_groups,
                room_ids=self.allowed_rooms):
            return True
        if self._whitelist is not None:
            sid, stype = _base._resolve_chat(source)
            try:
                return bool(sid) and self._whitelist.is_allowed(stype, sid)
            except Exception:
                logger.debug("LINE: whitelist store query failed", exc_info=True)
        return False

    def _group_requires_mention(self, chat_id: str) -> bool:
        if self._whitelist is None:
            return True
        try:
            return bool(self._whitelist.requires_mention(chat_id))
        except Exception:
            return True

    def _warn_mention_gate_unavailable(self) -> None:
        """Warn (once per connection) that the @mention gate cannot be enforced, so it has failed open."""
        if self._mention_gate_warned:
            return
        self._mention_gate_warned = True
        logger.warning(
            "LINE: requires_mention is enabled but the bot userId is unknown (GET /v2/bot/info failed at "
            "connect) — @mention detection is impossible, so the mention gate is DISABLED (fail-open): "
            "authorized groups will keep receiving replies WITHOUT an @mention. Restore connectivity to "
            "https://api.line.me/v2/bot/info (check LINE_CHANNEL_ACCESS_TOKEN) and reconnect to re-enable "
            "mention gating.")

    async def _send_plain(self, chat_id: str, reply_token: str, text: str) -> None:
        """Send one plain-text bubble via reply (preferred) or push fallback."""
        if not self._client or not text:
            return
        messages = [_base._text_message(text)]
        if reply_token:
            try:
                await self._client.reply(reply_token, messages)
                return
            except Exception:
                logger.debug("LINE: plain reply failed, trying push", exc_info=True)
        try:
            await self._client.push(chat_id, messages)
        except Exception:
            logger.debug("LINE: plain push failed", exc_info=True)

    def _load_gateway_config(self):
        try:
            from gateway.config import load_gateway_config
            return load_gateway_config()
        except Exception:
            logger.debug("LINE: load_gateway_config failed", exc_info=True)
            return None

    async def _notify_admin_unauthorized(self, chat_type: str, source_id: str, display: str = "") -> None:
        """Notify admins about an unauthorized/new source (deduped in the helper)."""
        if self._whitelist is None:
            return
        gw = self._load_gateway_config()
        if gw is None:
            return
        try:
            from .whitelist_notify import notify_unauthorized
            await notify_unauthorized(
                self._whitelist, gw, source_type=chat_type, source_id=source_id, display=display)
        except Exception:
            logger.debug("LINE: notify_unauthorized failed", exc_info=True)

    async def _source_display_name(self, chat_id: str, chat_type: str, user_id: str) -> str:
        """Best-effort display name for a SOURCE (pending-queue attribution): group name for a group/room
        source, the sender's profile name for a DM. ``''`` lets the caller substitute the raw id."""
        if chat_type == "dm":
            return await self._resolve_name(chat_id, chat_type, user_id) or ""
        return await self._resolve_chat_name(chat_id, chat_type) or ""

    def _record_pending(self, source_id: str, source_type: str, name: str) -> None:
        """Log an unauthorized attempt into the pending queue (best-effort)."""
        if self._whitelist is None:
            return
        try:
            self._whitelist.record_attempt(source_id, platform="line", source_type=source_type, name=name)
        except Exception:
            logger.debug("LINE: record_attempt failed", exc_info=True)

    async def _maybe_notify_new_source(self, source: Dict[str, Any]) -> None:
        sid, stype = _base._resolve_chat(source)
        if not sid:
            return
        name = await self._resolve_chat_name(sid, stype) or ""
        self._record_pending(sid, stype, name)
        await self._notify_admin_unauthorized(stype, sid, display=name or sid)

    async def _reject_unauthorized(
        self, *, chat_id: str, chat_type: str, user_id: str, reply_token: str, attempt_text: str, dm: bool,
    ) -> None:
        """Record the attempt (with resolved name), notify admins (dedup), send a throttled English reply."""
        name = await self._source_display_name(chat_id, chat_type, user_id)
        self._record_pending(chat_id, chat_type, name)
        await self._notify_admin_unauthorized(chat_type, chat_id, display=name or (user_id if dm else chat_id))

        if self._whitelist is not None:
            try:
                if not self._whitelist.should_reply_unauthorized(chat_id):
                    return
            except Exception:
                pass
        await self._send_plain(chat_id, reply_token, UNAUTH_DM_REPLY if dm else UNAUTH_GROUP_REPLY)
        if self._whitelist is not None:
            try:
                self._whitelist.mark_unauthorized_replied(chat_id)
            except Exception:
                logger.debug("LINE: mark_unauthorized_replied failed", exc_info=True)

    # -- name resolution (TTL-cached, best-effort) --------------------------

    def _name_cache_get(self, key: str) -> Optional[str]:
        hit = self._name_cache.get(key)
        if hit and hit[1] > time.time():
            return hit[0]
        return None

    def _name_cache_put(self, key: str, value: Optional[str]) -> None:
        if value:
            self._name_cache[key] = (value, time.time() + self._name_cache_ttl)

    async def _resolve_name(self, chat_id: str, chat_type: str, user_id: str) -> Optional[str]:
        """Display name of a user. DM → profile; group/room → member profile."""
        if not user_id or not self._client:
            return None
        ck = f"u:{chat_id}:{user_id}" if chat_type in {"group", "room"} else f"u:{user_id}"
        cached = self._name_cache_get(ck)
        if cached:
            return cached
        try:
            if chat_type == "dm":
                name = await self._client.get_profile(user_id)
            else:
                name = await self._client.get_member_name(chat_id, user_id, chat_type=chat_type)
        except Exception:
            name = None
        self._name_cache_put(ck, name)
        return name

    async def _resolve_chat_name(self, chat_id: str, chat_type: str) -> Optional[str]:
        if not chat_id or not self._client or chat_type != "group":
            return None
        ck = f"g:{chat_id}"
        cached = self._name_cache_get(ck)
        if cached:
            return cached
        try:
            name = await self._client.get_group_summary(chat_id)
        except Exception:
            name = None
        self._name_cache_put(ck, name)
        return name

    # -- passive observe recording + quote reply ------------------------------

    async def _observe_record(
        self, *, source: Dict[str, Any], chat_id: str, chat_type: str, user_id: str, msg: Dict[str, Any],
        msg_type: str, message_id: str,
    ) -> None:
        """Record a message as passive observed context (no agent turn).

        Media policy: drop video/audio entirely; record text. Images and files are recorded as a
        LIGHTWEIGHT ``[image]`` / ``[file: name]`` placeholder together with their LINE
        ``platform_message_id`` — NO download or extraction here. ``_backfill_recent_media`` re-fetches
        them on demand when the bot is later triggered. The observed rows land in a single shared,
        chat-scoped session (per-user identity dropped).
        """
        if not self._observe_unmentioned:
            return
        store = getattr(self, "_session_store", None)
        if store is None:
            return
        if msg_type in {"video", "audio"}:
            return  # dropped by policy — not recorded, not fetched
        if msg_type == "text":
            body = msg.get("text", "") or ""
        elif msg_type == "file":
            fname = msg.get("fileName", "")
            body = f"[file: {fname}]" if fname else "[file]"
        else:
            body = f"[{msg_type}]"
        if not body:
            return
        name = await self._resolve_name(chat_id, chat_type, user_id) or user_id
        content = f"[{name}|{user_id}]\n{body}"
        try:
            shared = self.build_source(
                chat_id=chat_id, chat_type=chat_type,
                chat_name=(await self._resolve_chat_name(chat_id, chat_type) or chat_id))
            entry = store.get_or_create_session(shared)
            store.append_to_transcript(entry.session_id, {
                "role": "user", "content": content, "observed": True, "platform_message_id": message_id})
        except Exception:
            logger.debug("LINE: observe-record failed", exc_info=True)

    def _backfill_window_seconds(self) -> float:
        """Live backfill window in seconds — dashboard-editable ``media_backfill_window_minutes`` (config,
        hot-reload), else the env/extra default. 0 disables backfill."""
        minutes = self._backfill_window_minutes_default
        wl = getattr(self, "_whitelist", None)
        if wl is not None:
            try:
                v = wl.get_settings().get("media_backfill_window_minutes")
                if v is not None:
                    minutes = float(v)
            except Exception:
                pass
        return max(0.0, minutes) * 60.0

    async def _recent_observed_context(self, chat_id: str, chat_type: str) -> Optional[str]:
        """Read shared passive context without merging per-user conversation histories."""
        store = getattr(self, "_session_store", None)
        if not self._observe_unmentioned or store is None:
            return None
        try:
            shared = self.build_source(chat_id=chat_id, chat_type=chat_type)
            entry = await asyncio.to_thread(store.get_or_create_session, shared)
            db = getattr(store, "_db", None)
            if db is None:
                return None
            rows = await asyncio.to_thread(db.get_messages, entry.session_id)
            observed = [str(row["content"]) for row in rows
                        if row.get("observed") and row.get("role") == "user" and row.get("content")]
            if not observed:
                return None
            body = "\n\n".join(observed[-20:])[-10000:]
            return "[Observed LINE group context - context only, not requests]\n" + body
        except Exception:
            logger.debug("LINE: passive context lookup failed", exc_info=True)
            return None

    def _bf_cache_get(self, cache: "OrderedDict", key: str):
        """FIFO-cache lookup that refreshes recency on hit."""
        if key in cache:
            cache.move_to_end(key)
            return cache[key]
        return None

    def _bf_cache_put(self, cache: "OrderedDict", key: str, value) -> None:
        cache[key] = value
        cache.move_to_end(key)
        while len(cache) > self._bf_cache_max:
            cache.popitem(last=False)

    async def _bf_download(self, message_id: str, kind: str, file_name: str = "") -> Optional[Tuple[str, str]]:
        """Download-once memoization for backfill: reuse the cached file for a LINE message id as long as it
        still exists on disk, else re-fetch."""
        hit = self._bf_cache_get(self._bf_download_cache, message_id)
        if hit is not None:
            path, mime = hit
            if path and os.path.exists(path):
                return hit
        try:
            downloaded = await self._download_media(message_id, kind, filename=file_name)
        except Exception:
            downloaded = None
        if downloaded and downloaded[0]:
            self._bf_cache_put(self._bf_download_cache, message_id, downloaded)
            return downloaded
        return None

    async def _bf_vision(self, message_id: str, path: str) -> Optional[str]:
        """Extract-once memoization: vision-read an image and cache the analysis text by the (immutable)
        LINE message id so re-pulling the same image into a later trigger turn never re-runs vision."""
        cached = self._bf_cache_get(self._bf_vision_cache, message_id)
        if cached is not None:
            return cached
        try:
            from tools.vision_tools import vision_analyze_tool
            raw = await vision_analyze_tool(
                image_url=path,
                user_prompt=(
                    "請描述這張圖片的內容。若是收據、帳單、發票或菜單，"
                    "逐項列出商家名稱、各品項與金額、稅/服務費與總金額（含幣別）。"))
            text = ""
            try:
                data = json.loads(raw) if isinstance(raw, str) else (raw or {})
                if data.get("success"):
                    text = str(data.get("analysis") or "").strip()
            except (ValueError, TypeError, AttributeError):
                text = str(raw or "").strip()
            if text:
                self._bf_cache_put(self._bf_vision_cache, message_id, text)
            return text or None
        except Exception:
            logger.debug("LINE: backfill vision failed", exc_info=True)
            return None

    async def _backfill_recent_media(
        self, chat_id: str, chat_type: str,
    ) -> Tuple[Optional[str], List[str], List[str]]:
        """On-demand, deterministic media backfill. When the bot is triggered but the triggering message
        carries no media of its own, look back at recently-OBSERVED image/file uploads in this group within
        the backfill window and make their content available to THIS turn.

        * Images are vision-read here (extract-once) and injected as ``channel_context`` text, so the raw
          image is NOT re-attached every turn (the gateway's vision pass has no cache and would re-bill it).
        * Files/PDFs are downloaded (download-once) and returned as ``media_urls``.

        Returns ``(channel_context, media_urls, media_types)``. Best-effort; never raises."""
        window = self._backfill_window_seconds()
        if window <= 0:
            return None, [], []
        store = getattr(self, "_session_store", None)
        if store is None or chat_type not in {"group", "room"}:
            return None, [], []
        _MAX_BACKFILL = 3
        try:
            shared = self.build_source(chat_id=chat_id, chat_type=chat_type)
            entry = store.get_or_create_session(shared)
            db = getattr(store, "_db", None)
            if db is None or not hasattr(db, "get_messages"):
                return None, [], []
            now = time.time()
            # (message_id, kind, hhmm, file_name)
            candidates: List[Tuple[str, str, str, str]] = []
            for row in db.get_messages(entry.session_id):
                if not row.get("observed"):
                    continue
                ts = row.get("timestamp")
                try:
                    if ts is not None and (now - float(ts)) > window:
                        continue
                    hhmm = time.strftime("%H:%M", time.localtime(float(ts))) if ts else "?"
                except (TypeError, ValueError):
                    continue
                mid = str(row.get("platform_message_id") or "")
                if not mid:
                    continue
                content = str(row.get("content") or "")
                if "[image]" in content:
                    candidates.append((mid, "image", hhmm, ""))
                elif "[file" in content:
                    # content tail is "[file: name.pdf]" — recover the name.
                    fname = ""
                    marker = "[file: "
                    if marker in content:
                        fname = content.split(marker, 1)[1].split("]", 1)[0].strip()
                    candidates.append((mid, "file", hhmm, fname))
            # Most recent first, capped.
            hint_lines: List[str] = []
            urls: List[str] = []
            types: List[str] = []
            for mid, kind, hhmm, fname in list(reversed(candidates))[:_MAX_BACKFILL]:
                downloaded = await self._bf_download(mid, kind, file_name=fname)
                if not downloaded:
                    continue
                path, mime = downloaded
                if kind == "image":
                    analysis = await self._bf_vision(mid, path)
                    if analysis:
                        hint_lines.append(f"- 圖片（{hhmm} 上傳）內容：{analysis}")
                    else:
                        hint_lines.append(f"- 圖片（{hhmm} 上傳）：無法辨識內容")
                else:
                    urls.append(path)
                    types.append(mime or kind)
                    label = fname or "檔案"
                    hint_lines.append(f"- 檔案 {label}（{hhmm} 上傳）：已附上本回合，可用檔案工具讀取")
            channel_context = None
            if hint_lines:
                channel_context = (
                    "[近期本群組上傳的媒體 / Recently uploaded media in this group]\n" + "\n".join(hint_lines))
                logger.info(
                    "LINE: backfilled %d recent observed media into trigger turn (chat %s, window %.0fs, "
                    "%d file attach)", len(hint_lines), chat_id, window, len(urls))
            return channel_context, urls, types
        except Exception:
            logger.debug("LINE: media backfill failed", exc_info=True)
            return None, [], []

    async def _quote_context(
        self, source: Dict[str, Any], chat_id: str, chat_type: str, msg: Dict[str, Any],
    ) -> Optional[str]:
        """If this message quotes an earlier one (``quotedMessageId``), look the original up in the
        transcript and return it as a context string. Degrades to a short marker if the original can't be
        found (unrecorded, or aged out of retention)."""
        qmid = (msg or {}).get("quotedMessageId")
        if not qmid:
            return None
        store = getattr(self, "_session_store", None)
        if store is None:
            return None
        try:
            if chat_type in {"group", "room"}:
                lookup_src = self.build_source(chat_id=chat_id, chat_type=chat_type)
            else:
                lookup_src = self.build_source(chat_id=chat_id, chat_type=chat_type, user_id=source.get("userId"))
            entry = store.get_or_create_session(lookup_src)
            db = getattr(store, "_db", None)
            if db is None or not hasattr(db, "get_messages"):
                return "(In reply to an earlier message.)"
            for row in db.get_messages(entry.session_id):
                if str(row.get("platform_message_id") or "") == str(qmid):
                    original = (row.get("content") or "").strip()
                    if original:
                        return f"[Quoted message]\n{original}"
                    break
        except Exception:
            logger.debug("LINE: quote lookup failed", exc_info=True)
        return "(In reply to an earlier message.)"


class _OverrideContext:
    """Delegate every registration to the real ``PluginContext``; only ``register_platform`` is
    intercepted, to swap the adapter factory. Upstream's kwargs (label, env names, install hint,
    limits, platform hint) are therefore reused verbatim instead of being copied here."""

    def __init__(self, ctx, **overrides: Any) -> None:
        self._ctx = ctx
        self._overrides = overrides

    def register_platform(self, *args: Any, **kwargs: Any):
        kwargs.update(self._overrides)
        return self._ctx.register_platform(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._ctx, name)


def register(ctx) -> None:
    """Plugin entry point: upstream's registration with this adapter, plus the ``line_whitelist`` tool."""
    _base.register(_OverrideContext(ctx, adapter_factory=lambda cfg: LineAdapter(cfg)))
    from .line_whitelist_tool import register_tool
    register_tool(ctx)
