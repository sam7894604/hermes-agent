"""
LINE Messaging API platform adapter for Hermes Agent.

A bundled platform plugin that runs an aiohttp webhook server, accepts LINE
webhook events (signature-verified), and relays messages to/from the agent
via the standard ``BasePlatformAdapter`` interface.

Design highlights
-----------------

**Reply token preferred, Push fallback.** LINE's reply token is single-use
and expires roughly 60 seconds after the inbound event. We try Reply first
(it's free) and fall back to the metered Push API when the token is absent,
expired, or rejected by the API.

**Slow-LLM postback button (optional).** When the LLM is still running past
``slow_response_threshold`` seconds (default 45, leaving 15s margin on the
60s reply-token TTL), we burn the original reply token to send a Template
Buttons bubble — the user taps it later to receive the cached answer via a
*fresh* reply token (also free). State machine: PENDING → READY → DELIVERED,
with ERROR for cancelled runs. Set the threshold to 0 to disable the
button and always Push-fallback instead.

**Three-allowlist gating.** Separate allowlists for users (U-prefixed),
groups (C-prefixed), and rooms (R-prefixed). ``LINE_ALLOW_ALL_USERS=true``
is a dev-only escape hatch.

**Media via public HTTPS.** LINE's Messaging API does *not* accept
binary uploads — images, audio, and video must be reachable HTTPS URLs.
We register registered tempfiles under ``/line/media/<token>/<filename>``
served by the same aiohttp app, with an allowed-roots traversal guard.
``LINE_PUBLIC_URL`` (e.g. ``https://my-tunnel.example.com``) overrides
the host:port construction so URLs are reachable when the bind is a
wildcard/dual-stack listener or behind a reverse proxy.

**5-message batching.** LINE accepts at most 5 message objects per
Reply/Push call; longer responses are smart-chunked at 4500 chars
(LINE per-bubble limit is 5000) and batched.

Synthesis credits
-----------------

This file is a synthesis of seven open community PRs adding LINE support
to Hermes Agent. It deliberately ports the *strongest* idea from each into
a single plugin-form module that requires zero core edits:

* PR #18153 (leepoweii)   — Template Buttons postback cache state machine,
  Markdown URL preservation, system-message bypass.
* PR #8398  (yuga-hashimoto) — media URL serving with traversal guard,
  send_voice / send_video, ``LINE_PUBLIC_URL`` env, macOS ``/tmp`` root.
* PR #16832 (jethac)      — config wiring style, voice/image tests.
* PR #21023 (perng)       — plugin-form skeleton (the only one already
  modeled on ``ADDING_A_PLATFORM.md``), reply→push fallback at 50s TTL,
  loading-animation indicator, source dispatcher.
* PR #14942 (soichiyo)    — Cloudflare-tunnel operating model (docs only).
* PR #14988 (David-0x221Eight) — text-first scope discipline.
* PR #6676  (liyoungc)    — Push-only mode (used as the ``threshold=0``
  fallback path here).
"""

from __future__ import annotations

import asyncio
import base64
import enum
import hashlib
import hmac
import json
import logging
import mimetypes
import os
import re
import secrets
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from collections import deque, OrderedDict
from typing import Any, Deque, Dict, List, Optional, Set, Tuple
from urllib.parse import quote as _urlquote

from agent.secret_scope import UnscopedSecretError as _UnscopedSecretError
from agent.secret_scope import get_secret as _scoped_get_secret


def _get_scoped_secret(name, default=None):
    """Scope-aware credential read with the default-profile startup fallback.

    Secondary profiles construct their adapters under a profile secret
    scope -- the scope is authoritative and a scoped miss returns ``default``
    (no cross-profile borrow from ``os.environ``, which may hold another
    profile's value). The DEFAULT profile's adapter constructs and sends
    *unscoped* under multiplexing, where a bare ``get_secret`` would raise
    ``UnscopedSecretError`` and crash this path; there ``os.environ`` is that
    profile's own value, so fall back to it. Same pattern as the Slack
    ``SLACK_APP_TOKEN`` read (#59739) and
    ``gateway/platforms/whatsapp_common.py::_get_wsecret``.
    """
    try:
        val = _scoped_get_secret(name, default)
    except _UnscopedSecretError:
        val = os.getenv(name)
    return val if val is not None else default


logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Lazy / function-level imports for gateway internals are NOT used here —
# the plugin discovery flow imports adapter.py late enough that gateway is
# already loaded.
# ---------------------------------------------------------------------------

from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
    cache_audio_from_bytes,
    cache_document_from_bytes,
    cache_image_from_bytes,
    cache_video_from_bytes,
)
from gateway.config import Platform
# LINE renders zero Markdown, so a GFM pipe table lands as literal "| a | b |"
# rows in the bubble. Reuse the SAME shared converter Discord and Telegram
# already use (PR #53284) rather than growing a LINE-specific one.
from gateway.platforms.helpers import convert_table_to_bullets

# Whitelist subsystem (Phase 1 — hot-reload store backed by config.yaml).
# Relative import in the normal package context; fall back to the absolute
# path when adapter.py is loaded as a standalone module (the test plugin
# loader executes it outside its parent package).
try:
    from .whitelist_store import WhitelistStore
except ImportError:  # pragma: no cover - standalone plugin-loader path
    from plugins.platforms.line.whitelist_store import WhitelistStore


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

LINE_REPLY_URL = "https://api.line.me/v2/bot/message/reply"
LINE_PUSH_URL = "https://api.line.me/v2/bot/message/push"
LINE_LOADING_URL = "https://api.line.me/v2/bot/chat/loading/start"
LINE_CONTENT_URL_FMT = "https://api-data.line.me/v2/bot/message/{message_id}/content"
LINE_BOT_INFO_URL = "https://api.line.me/v2/bot/info"
# Name resolution (whitelist subsystem — display names for Dashboard / observed context)
LINE_PROFILE_URL_FMT = "https://api.line.me/v2/bot/profile/{user_id}"
LINE_GROUP_SUMMARY_URL_FMT = "https://api.line.me/v2/bot/group/{group_id}/summary"
LINE_GROUP_MEMBER_URL_FMT = "https://api.line.me/v2/bot/group/{group_id}/member/{user_id}"
LINE_ROOM_MEMBER_URL_FMT = "https://api.line.me/v2/bot/room/{room_id}/member/{user_id}"

# LINE Messaging API hard limits
LINE_PER_BUBBLE_CHARS = 5000  # Hard limit per text message object
LINE_SAFE_BUBBLE_CHARS = 4500  # Conservative limit for chunking
LINE_MAX_MESSAGES_PER_CALL = 5  # API rejects >5 messages per Reply/Push
LINE_REPLY_TOKEN_TTL_SECONDS = 50  # Conservative cap below LINE's ~60s

# Webhook hardening
WEBHOOK_BODY_MAX_BYTES = 1_048_576  # 1 MiB — webhooks are tiny JSON
DEFAULT_WEBHOOK_PORT = 8646
DEFAULT_WEBHOOK_PATH = "/line/webhook"
DEFAULT_MEDIA_PATH_PREFIX = "/line/media"

# Default bind host. ``None`` tells aiohttp/asyncio's ``create_server`` to
# bind BOTH address families (IPv4 + IPv6) — the portable dual-stack default.
# Mirrors gateway/platforms/webhook.py DEFAULT_HOST (commit d542894ad).
#
# Why not "0.0.0.0" (the old default) or "::"?
#   - "0.0.0.0" binds IPv4 ONLY. On IPv6-only private networks — notably
#     Fly.io 6PN, where the hosted edge router reverse-proxies LINE ingest to
#     ``<app>.internal:8646`` over an ``fdaa:…`` IPv6 address — an IPv4-only
#     listener is unreachable: dial refused → customer-visible 502 (NS-603).
#   - "::" is NOT a safe fix: on hosts where the kernel sets IPV6_V6ONLY=1
#     (verified on Fly machines), binding "::" yields an IPv6-ONLY socket,
#     breaking IPv4 loopback health probes.
#   - ``None`` asks the event loop to create a listening socket per resolved
#     family, so both 127.0.0.1 (v4) and the 6PN fdaa (v6) are served
#     regardless of the bindv6only sysctl. Users can still pin a host via
#     ``LINE_HOST`` or ``platforms.line.extra.host``.
DEFAULT_HOST = None

# Hosts that mean "listening on every interface" — i.e. the bind address is
# not a name LINE's servers could ever fetch media from, so a public base URL
# is required for outbound media.
_WILDCARD_HOSTS = frozenset({"0.0.0.0", "::", ""})

# Slow-LLM postback button defaults
DEFAULT_SLOW_RESPONSE_THRESHOLD = 45.0  # seconds; 0 disables
DEFAULT_PENDING_REPLY_TEXT = (
    "🤔 Still thinking. Tap below to fetch the answer when it's ready."
)
DEFAULT_BUTTON_LABEL = "Get answer"
DEFAULT_DELIVERED_TEXT = "Already replied ✅"
DEFAULT_INTERRUPTED_TEXT = "Run was interrupted before completion."

# Media defaults
MEDIA_TOKEN_TTL_SECONDS = 1800  # 30 minutes; LINE caches the URL aggressively
LINE_IMAGE_MAX_BYTES = 10 * 1024 * 1024  # 10 MB per LINE docs
LINE_AV_MAX_BYTES = 200 * 1024 * 1024  # 200 MB for voice/video

# Map LINE webhook message types to the normalized MessageType the gateway
# routes on. LINE has no separate "voice" type — audio messages are recorded
# voice clips, so they map to VOICE (which the gateway sends through STT),
# mirroring how Telegram/WhatsApp classify voice notes. Anything unknown
# falls back to TEXT.
_LINE_MESSAGE_TYPES = {
    "text": MessageType.TEXT,
    "image": MessageType.PHOTO,
    "video": MessageType.VIDEO,
    "audio": MessageType.VOICE,
    "file": MessageType.DOCUMENT,
    "location": MessageType.LOCATION,
    "sticker": MessageType.STICKER,
}

# A 1×1 transparent PNG used as fallback video preview thumbnail when no
# explicit preview is supplied — LINE requires ``previewImageUrl`` for
# video messages. Sourced from the Python stdlib (no Pillow dependency).
_FALLBACK_PNG_PREVIEW = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000d49444154789c63000100000005000100377a7ff20000000049454e"
    "44ae426082"
)


# ---------------------------------------------------------------------------
# Markdown stripping (URL-preserving)
# ---------------------------------------------------------------------------

_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)")
_MD_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_MD_ITAL_RE = re.compile(r"(?<!\*)\*(?!\s)(.+?)(?<!\s)\*(?!\*)")
_MD_CODE_INLINE_RE = re.compile(r"`([^`]+)`")
_MD_CODE_BLOCK_RE = re.compile(r"```[a-zA-Z0-9_+-]*\n?(.*?)```", re.DOTALL)
_MD_HEADING_RE = re.compile(r"^#{1,6}\s+", re.MULTILINE)
_MD_BULLET_RE = re.compile(r"^[\s]*[-*+]\s+", re.MULTILINE)


def strip_markdown_preserving_urls(text: str) -> str:
    """Strip Markdown that LINE can't render, but keep URLs usable.

    LINE's text bubble has zero Markdown support — bold, italics, code
    fences, headings, and bullet markers all render as literal characters.
    URLs *are* auto-linked by the client, but only when they appear bare
    (not inside ``[label](url)`` syntax). This converts ``[label](url)``
    to ``label (url)`` so the URL remains tappable, then strips the rest.

    Source: PR #18153 (leepoweii) — adapted to keep code-block content
    visible (LINE users frequently want command snippets to land as
    plain text, not be eaten by the fence).

    Markdown tables are handled first by the shared ``convert_table_to_bullets``
    (the same one Discord and Telegram call) — LINE has no table syntax either,
    so a pipe table would otherwise survive this function as literal ``|`` rows.
    """
    if not text:
        return text

    # Tables → bullet groups, via the shared cross-platform converter.
    # MUST run before the un-fencing below: the converter deliberately skips
    # fenced code blocks, but once the fences are stripped a table *inside* a
    # code block would look like a real table and be wrongly converted.
    # The converter emits "**heading**" + "• field: value"; the bold markers
    # are stripped below and the "•" bullets pass through _MD_BULLET_RE
    # untouched (it only matches -/*/+ markers), so the two compose cleanly.
    text = convert_table_to_bullets(text)

    # Code blocks first — keep the inner content, drop the fences.
    def _unfence(m: re.Match) -> str:
        return m.group(1).rstrip("\n")
    text = _MD_CODE_BLOCK_RE.sub(_unfence, text)

    # Inline code: keep content, drop backticks.
    text = _MD_CODE_INLINE_RE.sub(r"\1", text)

    # Markdown links → "label (url)"
    text = _MD_LINK_RE.sub(lambda m: f"{m.group(1)} ({m.group(2)})", text)

    # Bold/italic markers — strip.
    text = _MD_BOLD_RE.sub(r"\1", text)
    text = _MD_ITAL_RE.sub(r"\1", text)

    # Headings (#, ##) and bullet markers — strip the prefix only.
    text = _MD_HEADING_RE.sub("", text)
    text = _MD_BULLET_RE.sub("• ", text)

    return text


def split_for_line(text: str, max_chars: int = LINE_SAFE_BUBBLE_CHARS) -> List[str]:
    """Split ``text`` into LINE-sized bubbles, preferring paragraph/line breaks.

    Returns at most ``LINE_MAX_MESSAGES_PER_CALL`` chunks; longer text is
    truncated with an ellipsis on the final chunk to keep the response
    deliverable in a single Reply/Push call.
    """
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]

    chunks: List[str] = []
    remaining = text
    while remaining and len(chunks) < LINE_MAX_MESSAGES_PER_CALL:
        if len(remaining) <= max_chars:
            chunks.append(remaining)
            remaining = ""
            break
        # Try to break on the latest paragraph or newline within budget.
        cut = remaining.rfind("\n\n", 0, max_chars)
        if cut < int(max_chars * 0.5):
            cut = remaining.rfind("\n", 0, max_chars)
        if cut < int(max_chars * 0.5):
            cut = remaining.rfind(" ", 0, max_chars)
        if cut <= 0:
            cut = max_chars
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()

    if remaining:
        # Truncate gracefully — caller already burned its 5-bubble budget.
        if chunks:
            tail = chunks[-1]
            if len(tail) > max_chars - 1:
                tail = tail[: max_chars - 1]
            chunks[-1] = tail.rstrip() + "…"
        else:
            chunks.append(remaining[: max_chars - 1] + "…")
    return chunks


# ---------------------------------------------------------------------------
# Webhook signature verification
# ---------------------------------------------------------------------------

def verify_line_signature(body: bytes, signature: str, channel_secret: str) -> bool:
    """Verify a LINE webhook's ``X-Line-Signature`` header.

    LINE signs the *raw* request body with HMAC-SHA256 keyed by the
    channel secret, then base64-encodes the digest. Constant-time
    comparison defends against timing oracles.
    """
    if not signature or not channel_secret or body is None:
        return False
    try:
        digest = hmac.new(
            channel_secret.encode("utf-8"),
            body,
            hashlib.sha256,
        ).digest()
        expected = base64.b64encode(digest).decode("utf-8")
    except Exception:
        return False
    # Compare as bytes: compare_digest raises TypeError on a str with
    # non-ASCII characters, and the signature is a raw request header.
    return hmac.compare_digest(expected.encode(), signature.encode())


# ---------------------------------------------------------------------------
# Cache state machine — slow-LLM postback flow
# ---------------------------------------------------------------------------

class State(enum.Enum):
    PENDING = "pending"  # button sent, LLM still running
    READY = "ready"      # LLM done, response cached, waiting for postback tap
    DELIVERED = "delivered"
    ERROR = "error"      # LLM raised / interrupted; cached error text waiting


@dataclass
class _CacheEntry:
    state: State
    payload: Any = None
    chat_id: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)


class RequestCache:
    """In-memory cache for slow-LLM postback retrieval.

    PRs #18153 originally combined two TTLs — one for PENDING (24h) and
    a shorter one for READY/DELIVERED/ERROR (1h). We keep the same model
    here.
    """

    def __init__(
        self,
        ttl_seconds: int = 3600,
        pending_ttl_seconds: int = 86400,
    ) -> None:
        self._entries: Dict[str, _CacheEntry] = {}
        self._ttl = ttl_seconds
        self._pending_ttl = pending_ttl_seconds

    def register_pending(self, chat_id: str) -> str:
        rid = str(uuid.uuid4())
        self._entries[rid] = _CacheEntry(state=State.PENDING, chat_id=chat_id)
        return rid

    def get(self, request_id: str) -> Optional[_CacheEntry]:
        return self._entries.get(request_id)

    def set_ready(self, request_id: str, payload: Any) -> None:
        entry = self._entries.get(request_id)
        if entry is None or entry.state is not State.PENDING:
            return
        entry.state = State.READY
        entry.payload = payload
        entry.updated_at = time.time()

    def set_error(self, request_id: str, message: str) -> None:
        entry = self._entries.get(request_id)
        if entry is None or entry.state is not State.PENDING:
            return
        entry.state = State.ERROR
        entry.payload = message
        entry.updated_at = time.time()

    def mark_delivered(self, request_id: str) -> None:
        entry = self._entries.get(request_id)
        if entry is None or entry.state not in {State.READY, State.ERROR}:
            return
        entry.state = State.DELIVERED
        entry.updated_at = time.time()

    def find_pending_for_chat(self, chat_id: str) -> Optional[str]:
        for rid, entry in self._entries.items():
            if entry.state is State.PENDING and entry.chat_id == chat_id:
                return rid
        return None

    def prune(self) -> int:
        now = time.time()
        removed = 0
        for rid in list(self._entries.keys()):
            entry = self._entries[rid]
            if entry.state is State.PENDING:
                if now - entry.created_at > self._pending_ttl:
                    del self._entries[rid]
                    removed += 1
            else:
                if now - entry.updated_at > self._ttl:
                    del self._entries[rid]
                    removed += 1
        return removed


# ---------------------------------------------------------------------------
# Inbound dedup
# ---------------------------------------------------------------------------

class _MessageDeduplicator:
    """Bounded LRU of LINE webhook event IDs to ignore at-least-once retries."""

    def __init__(self, max_size: int = 1000) -> None:
        self._seen: Dict[str, float] = {}
        self._max = max_size

    def is_duplicate(self, event_id: str) -> bool:
        if not event_id:
            return False
        if event_id in self._seen:
            return True
        if len(self._seen) >= self._max:
            # Drop the oldest 10% so we don't trim on every insert.
            cutoff = sorted(self._seen.values())[len(self._seen) // 10 or 1]
            self._seen = {k: v for k, v in self._seen.items() if v > cutoff}
        self._seen[event_id] = time.time()
        return False


# ---------------------------------------------------------------------------
# Source / chat-id resolution
# ---------------------------------------------------------------------------

def _resolve_chat(source: Dict[str, Any]) -> Tuple[str, str]:
    """Return ``(chat_id, chat_type)`` from a LINE event ``source`` block.

    LINE sources are one of:
      * ``{"type": "user",  "userId":  "U..."}``  → 1:1 DM
      * ``{"type": "group", "groupId": "C...", "userId": "U..."}``  → group chat
      * ``{"type": "room",  "roomId":  "R...", "userId": "U..."}``  → multi-user room

    Source: PR #21023 (perng), unchanged.
    """
    src_type = (source or {}).get("type", "")
    if src_type == "group":
        return source.get("groupId", ""), "group"
    if src_type == "room":
        return source.get("roomId", ""), "room"
    if src_type == "user":
        return source.get("userId", ""), "dm"
    return "", "dm"


def _allowed_for_source(
    source: Dict[str, Any],
    *,
    allow_all: bool,
    user_ids: Set[str],
    group_ids: Set[str],
    room_ids: Set[str],
) -> bool:
    """Three-list gate — credit PR #18153."""
    if allow_all:
        return True
    src_type = (source or {}).get("type", "")
    if src_type == "user":
        uid = source.get("userId", "")
        return bool(uid) and uid in user_ids
    if src_type == "group":
        gid = source.get("groupId", "")
        return bool(gid) and gid in group_ids
    if src_type == "room":
        rid = source.get("roomId", "")
        return bool(rid) and rid in room_ids
    return False


# Unauthorized-source English replies (whitelist subsystem §2.6 / §2.7).
UNAUTH_GROUP_REPLY = (
    "This group isn't authorized to use the assistant yet. "
    "An administrator has been notified — please wait for approval."
)
UNAUTH_DM_REPLY = (
    "You're not authorized to use this assistant yet. "
    "An administrator has been notified — please contact the admin for access."
)


def _message_text(msg: Dict[str, Any]) -> str:
    """Best-effort plain-text extraction of an inbound message (for logging)."""
    if (msg or {}).get("type") == "text":
        return msg.get("text", "") or ""
    return f"[{(msg or {}).get('type', 'unknown')}]"


def _bot_mentioned(msg: Dict[str, Any], bot_user_id: Optional[str]) -> bool:
    """True if this text message @-mentions the bot.

    LINE puts mentions at ``message.mention.mentionees[]``; each entry carries
    ``isSelf`` (bool) and ``userId``. A ``type == "all"`` (@everyone) entry is
    NOT treated as addressing the bot specifically.
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


# ---------------------------------------------------------------------------
# LINE Reply / Push HTTP client
# ---------------------------------------------------------------------------

class _LineClient:
    """Thin async wrapper around the LINE Messaging API.

    We use ``aiohttp`` directly to avoid a ``line-bot-sdk`` dependency
    (the SDK pulls in its own httpx pin and the ergonomic gain is small
    for the four endpoints we actually call).
    """

    def __init__(self, channel_access_token: str, *, timeout: float = 15.0) -> None:
        self._token = channel_access_token
        self._timeout = timeout
        self._headers = {
            "Authorization": f"Bearer {channel_access_token}",
            "Content-Type": "application/json",
        }
        # Ids of messages this bot has sent (from the reply/push response
        # ``sentMessages``). Used to treat a quote-reply of the bot's OWN
        # message as an implicit @mention. Bounded so it can't grow unbounded.
        self.sent_message_ids: Deque[str] = deque(maxlen=500)

    def _record_sent(self, payload: Any) -> None:
        try:
            for m in (payload or {}).get("sentMessages", []) or []:
                mid = m.get("id")
                if mid:
                    self.sent_message_ids.append(str(mid))
        except Exception:
            pass

    async def reply(self, reply_token: str, messages: List[Dict[str, Any]]) -> None:
        import aiohttp
        timeout = aiohttp.ClientTimeout(total=self._timeout)
        async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
            async with session.post(
                LINE_REPLY_URL,
                headers=self._headers,
                json={"replyToken": reply_token, "messages": messages},
            ) as resp:
                if resp.status >= 400:
                    body = await resp.text()
                    raise RuntimeError(f"LINE reply {resp.status}: {body[:200]}")
                try:
                    self._record_sent(await resp.json())
                except Exception:
                    pass

    async def push(self, chat_id: str, messages: List[Dict[str, Any]]) -> None:
        import aiohttp
        timeout = aiohttp.ClientTimeout(total=self._timeout)
        async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
            async with session.post(
                LINE_PUSH_URL,
                headers=self._headers,
                json={"to": chat_id, "messages": messages},
            ) as resp:
                if resp.status >= 400:
                    body = await resp.text()
                    raise RuntimeError(f"LINE push {resp.status}: {body[:200]}")
                try:
                    self._record_sent(await resp.json())
                except Exception:
                    pass

    async def loading(self, chat_id: str, seconds: int = 60) -> None:
        """Loading indicator (DM only). LINE rejects this for groups/rooms."""
        if not chat_id or not chat_id.startswith("U"):
            return
        import aiohttp
        # LINE caps loadingSeconds in 5-step increments, max 60.
        clamped = max(5, min(60, (seconds // 5) * 5 or 5))
        try:
            timeout = aiohttp.ClientTimeout(total=5.0)
            async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
                await session.post(
                    LINE_LOADING_URL,
                    headers=self._headers,
                    json={"chatId": chat_id, "loadingSeconds": clamped},
                )
        except Exception as exc:  # best-effort; never raise
            logger.debug("LINE loading indicator failed: %s", exc)

    async def fetch_content(self, message_id: str) -> bytes:
        """Download an inbound media message's binary content."""
        import aiohttp
        url = LINE_CONTENT_URL_FMT.format(message_id=message_id)
        timeout = aiohttp.ClientTimeout(total=30.0)
        async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
            async with session.get(url, headers={"Authorization": f"Bearer {self._token}"}) as resp:
                if resp.status >= 400:
                    raise RuntimeError(f"LINE content {resp.status}")
                return await resp.read()

    async def get_bot_user_id(self) -> Optional[str]:
        """Fetch this channel's own userId so we can filter self-messages."""
        import aiohttp
        timeout = aiohttp.ClientTimeout(total=10.0)
        try:
            async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
                async with session.get(LINE_BOT_INFO_URL, headers=self._headers) as resp:
                    if resp.status >= 400:
                        return None
                    data = await resp.json()
                    return data.get("userId")
        except Exception:
            return None

    async def _get_json_field(self, url: str, field: str) -> Optional[str]:
        """Shared GET helper for name-resolution endpoints. Best-effort."""
        import aiohttp
        timeout = aiohttp.ClientTimeout(total=10.0)
        try:
            async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
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
        return await self._get_json_field(
            LINE_PROFILE_URL_FMT.format(user_id=user_id), "displayName"
        )

    async def get_group_summary(self, group_id: str) -> Optional[str]:
        """Group name (``GET /v2/bot/group/{id}/summary``)."""
        if not group_id:
            return None
        return await self._get_json_field(
            LINE_GROUP_SUMMARY_URL_FMT.format(group_id=group_id), "groupName"
        )

    async def get_member_name(
        self, chat_id: str, user_id: str, *, chat_type: str = "group"
    ) -> Optional[str]:
        """Display name of a member inside a group/room.

        LINE's plain ``/profile`` endpoint does not work for arbitrary group
        members — the group/room member endpoint is required.
        """
        if not chat_id or not user_id:
            return None
        if chat_type == "room":
            url = LINE_ROOM_MEMBER_URL_FMT.format(room_id=chat_id, user_id=user_id)
        else:
            url = LINE_GROUP_MEMBER_URL_FMT.format(group_id=chat_id, user_id=user_id)
        return await self._get_json_field(url, "displayName")


# ---------------------------------------------------------------------------
# Message builders
# ---------------------------------------------------------------------------

def _text_message(text: str) -> Dict[str, Any]:
    """Build a LINE text message object, capped to per-bubble max."""
    if len(text) > LINE_PER_BUBBLE_CHARS:
        text = text[: LINE_PER_BUBBLE_CHARS - 1] + "…"
    return {"type": "text", "text": text}


def _image_message(original_url: str, preview_url: Optional[str] = None) -> Dict[str, Any]:
    return {
        "type": "image",
        "originalContentUrl": original_url,
        "previewImageUrl": preview_url or original_url,
    }


def _audio_message(url: str, duration_ms: int = 1000) -> Dict[str, Any]:
    return {
        "type": "audio",
        "originalContentUrl": url,
        "duration": int(duration_ms),
    }


def _video_message(url: str, preview_url: str) -> Dict[str, Any]:
    return {
        "type": "video",
        "originalContentUrl": url,
        "previewImageUrl": preview_url,
    }


def build_postback_button_message(
    text: str, button_label: str, request_id: str
) -> Dict[str, Any]:
    """Template Buttons message — the slow-LLM postback bubble.

    From PR #18153 (leepoweii). Template Buttons stay tappable from chat
    history, unlike Quick Reply chips which are dismissed the moment any
    new message arrives in the chat.

    LINE limits: ``text`` ≤ 160 chars, ``altText`` ≤ 400 chars.
    """
    truncated = text if len(text) <= 160 else text[:157] + "..."
    alt = text if len(text) <= 400 else text[:397] + "..."
    return {
        "type": "template",
        "altText": alt,
        "template": {
            "type": "buttons",
            "text": truncated,
            "actions": [
                {
                    "type": "postback",
                    "label": button_label[:20] or "Get answer",
                    "data": json.dumps(
                        {"action": "show_response", "request_id": request_id}
                    ),
                    "displayText": button_label[:300] or "Get answer",
                }
            ],
        },
    }


# Prefixes the gateway uses for system busy-acks (interrupting / queued /
# steered). When the postback cache has a PENDING entry we *bypass* the
# cache for these so they reach the user as visible bubbles instead of
# being silently swallowed. From PR #18153.
_SYSTEM_BYPASS_PREFIXES: Tuple[str, ...] = (
    "⚡ Interrupting",
    "⏳ Queued",
    "⏩ Steered",
    "💾",  # background-review summary
)


def _is_system_bypass(content: str) -> bool:
    if not content:
        return False
    return any(content.startswith(p) for p in _SYSTEM_BYPASS_PREFIXES)


# ---------------------------------------------------------------------------
# Configuration helpers
# ---------------------------------------------------------------------------

def _csv_set(value: str) -> Set[str]:
    if not value:
        return set()
    return {x.strip() for x in value.split(",") if x.strip()}


def _truthy_env(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "on"}


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------

class LineAdapter(BasePlatformAdapter):
    """LINE Messaging API gateway adapter."""

    # LINE has its own message-edit story (none) — we always send fresh
    # bubbles, never edit, so REQUIRES_EDIT_FINALIZE stays False.

    def __init__(self, config, **kwargs):
        platform = Platform("line")
        super().__init__(config=config, platform=platform)

        extra = getattr(config, "extra", {}) or {}

        # Credentials
        self.channel_access_token = (
            _get_scoped_secret("LINE_CHANNEL_ACCESS_TOKEN")
            or extra.get("channel_access_token", "")
        )
        self.channel_secret = (
            _get_scoped_secret("LINE_CHANNEL_SECRET")
            or extra.get("channel_secret", "")
        )

        # Webhook server. Host default is ``None`` → dual-stack bind (both
        # IPv4 and IPv6); see DEFAULT_HOST above. ``LINE_HOST``/extra.host pin
        # a specific address when needed; empty string collapses to None.
        self.webhook_host = (
            os.getenv("LINE_HOST") or extra.get("host", DEFAULT_HOST) or DEFAULT_HOST
        )
        try:
            self.webhook_port = int(
                os.getenv("LINE_PORT") or extra.get("port", DEFAULT_WEBHOOK_PORT)
            )
        except (TypeError, ValueError):
            self.webhook_port = DEFAULT_WEBHOOK_PORT
        self.webhook_path = extra.get("webhook_path", DEFAULT_WEBHOOK_PATH)

        # Public base URL — required for media sending when bind isn't
        # publicly reachable.
        self.public_base_url = (
            os.getenv("LINE_PUBLIC_URL")
            or extra.get("public_url", "")
            or ""
        ).rstrip("/")

        # Three-allowlist gating
        self.allow_all = _truthy_env(
            "LINE_ALLOW_ALL_USERS", bool(extra.get("allow_all_users", False))
        )
        self.allowed_users = _csv_set(
            os.getenv("LINE_ALLOWED_USERS", "")
        ) | set(extra.get("allowed_users", []))
        self.allowed_groups = _csv_set(
            os.getenv("LINE_ALLOWED_GROUPS", "")
        ) | set(extra.get("allowed_groups", []))
        self.allowed_rooms = _csv_set(
            os.getenv("LINE_ALLOWED_ROOMS", "")
        ) | set(extra.get("allowed_rooms", []))

        # Whitelist subsystem (Phase 1): hot-reload store backed by
        # config.yaml (platforms.line.*). The static env allowlists above are
        # kept as a backward-compatible overlay — a source is authorized if it
        # matches EITHER the env sets OR the live config store (so existing
        # env-only deployments keep working while new entries hot-reload).
        try:
            self._whitelist: Optional[WhitelistStore] = WhitelistStore()
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "LINE: WhitelistStore init failed (%s); env allowlists only", exc
            )
            self._whitelist = None
        # Passive-context (observed) group recording — §6. Default on; the
        # store/config `observe_unmentioned` flag can disable per deployment.
        self._observe_unmentioned = _truthy_env(
            "LINE_OBSERVE_UNMENTIONED",
            bool(extra.get("observe_unmentioned", True)),
        )
        # On-demand media backfill: observed (unmentioned) uploads are recorded
        # as a lightweight placeholder ONLY; when the bot IS later triggered and
        # the triggering message carries no media of its own, we look back at the
        # recently-observed image/file uploads within this window and pull them
        # into the current turn. Env default here; the live value comes from the
        # Dashboard-editable ``media_backfill_window_minutes`` (see the store).
        self._backfill_window_minutes_default = float(
            os.getenv("LINE_MEDIA_BACKFILL_WINDOW_MIN")
            or extra.get("media_backfill_window_minutes", 1)
        )
        # Backfill cost control — "抽過的快取不重抽": the same recently-uploaded
        # media can be pulled into several trigger turns inside the window, so we
        # memoize both the LINE download and the vision extraction by the
        # (immutable) LINE message id. Bounded FIFO so a busy group can't grow
        # them without limit.
        self._bf_download_cache: "OrderedDict[str, Tuple[str, str]]" = OrderedDict()
        self._bf_vision_cache: "OrderedDict[str, str]" = OrderedDict()
        self._bf_cache_max = int(
            os.getenv("LINE_MEDIA_BACKFILL_CACHE_MAX")
            or extra.get("media_backfill_cache_max", 256)
        )
        # Name-resolution TTL cache: id -> (display_name, expiry_ts).
        self._name_cache: Dict[str, Tuple[str, float]] = {}
        self._name_cache_ttl = float(
            os.getenv("LINE_NAME_CACHE_TTL") or extra.get("name_cache_ttl", 3600)
        )

        # Slow-LLM postback button threshold
        try:
            self.slow_response_threshold = float(
                os.getenv("LINE_SLOW_RESPONSE_THRESHOLD")
                or extra.get("slow_response_threshold", DEFAULT_SLOW_RESPONSE_THRESHOLD)
            )
        except (TypeError, ValueError):
            self.slow_response_threshold = DEFAULT_SLOW_RESPONSE_THRESHOLD

        # User-overridable copy
        self.pending_text = (
            os.getenv("LINE_PENDING_TEXT")
            or extra.get("pending_text", DEFAULT_PENDING_REPLY_TEXT)
        )
        self.button_label = (
            os.getenv("LINE_BUTTON_LABEL")
            or extra.get("button_label", DEFAULT_BUTTON_LABEL)
        )
        self.delivered_text = (
            os.getenv("LINE_DELIVERED_TEXT")
            or extra.get("delivered_text", DEFAULT_DELIVERED_TEXT)
        )
        self.interrupted_text = (
            os.getenv("LINE_INTERRUPTED_TEXT")
            or extra.get("interrupted_text", DEFAULT_INTERRUPTED_TEXT)
        )

        # Runtime state
        self._client: Optional[_LineClient] = None
        self._app = None  # aiohttp.web.Application
        self._runner = None  # aiohttp.web.AppRunner
        self._site = None  # aiohttp.web.TCPSite
        self._reply_tokens: Dict[str, Tuple[str, float]] = {}  # chat_id → (token, expiry)
        self._cache = RequestCache()
        self._dedup = _MessageDeduplicator()
        self._bot_user_id: Optional[str] = None
        # One-shot flag so the fail-open mention-gate warning is logged once
        # per connection, not once per inbound message.
        self._mention_gate_warned: bool = False
        self._lock_key: Optional[str] = None

        # Media state
        self._media_tokens: Dict[str, Tuple[str, float]] = {}  # token → (path, expiry)
        self._media_temp_paths: Set[str] = set()
        self._media_ttl = MEDIA_TOKEN_TTL_SECONDS

        # Pending-button slot per chat — ensures one outstanding postback
        # button per chat at a time. Postback cache request_id keyed by chat_id.
        self._pending_buttons: Dict[str, str] = {}

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        if not self.channel_access_token or not self.channel_secret:
            self._set_fatal_error(
                "config_missing",
                "LINE_CHANNEL_ACCESS_TOKEN and LINE_CHANNEL_SECRET must be set",
                retryable=False,
            )
            return False

        # Prevent two profiles from running on the same channel access token.
        try:
            from gateway.status import acquire_scoped_lock
            # Use a hash of the token so we don't write the secret to disk.
            tok_hash = hashlib.sha256(self.channel_access_token.encode()).hexdigest()[:16]
            if not acquire_scoped_lock("line", tok_hash):
                self._set_fatal_error(
                    "lock_conflict",
                    "LINE channel already in use by another profile",
                    retryable=False,
                )
                return False
            self._lock_key = tok_hash
        except ImportError:
            self._lock_key = None

        self._client = _LineClient(self.channel_access_token)

        # Best-effort: fetch our own bot userId for self-message filtering.
        # If the call fails (offline tests, transient 5xx) we fall back to
        # not filtering self-events; the cost is minor (LINE doesn't
        # actually echo our own messages back).
        self._mention_gate_warned = False  # fresh warning per connection cycle
        try:
            self._bot_user_id = await self._client.get_bot_user_id()
        except Exception as exc:
            logger.debug("LINE: get_bot_user_id failed: %s", exc)
            self._bot_user_id = None
        if self._bot_user_id:
            logger.info(
                "LINE: bot userId resolved (%s…) — @mention gate active",
                self._bot_user_id[:8],
            )
        else:
            logger.warning(
                "LINE: bot userId NOT resolved at connect — @mention gate will "
                "fail-open (authorized groups reply without @mention)."
            )

        # Spin up the aiohttp webhook server.
        try:
            from aiohttp import web
        except ImportError:
            self._set_fatal_error(
                "missing_dep",
                "aiohttp is required for the LINE adapter — install with `pip install aiohttp`",
                retryable=False,
            )
            return False

        self._app = web.Application(client_max_size=WEBHOOK_BODY_MAX_BYTES)
        self._app.router.add_post(self.webhook_path, self._handle_webhook)
        # Public health probe — useful for tunnel/proxy verification.
        self._app.router.add_get(f"{self.webhook_path}/health", self._handle_health)
        # Media serving endpoint.
        self._app.router.add_get(
            f"{DEFAULT_MEDIA_PATH_PREFIX}/{{token}}/{{filename}}",
            self._handle_media,
        )

        self._runner = web.AppRunner(self._app)
        try:
            await self._runner.setup()
            # SO_REUSEADDR is platform-dependent (mirrors the generic webhook
            # adapter, commits d542894ad/9420ad946):
            #   - macOS (BSD semantics): two wildcard/specific sockets with
            #     SO_REUSEADDR can silently split traffic — disable it there.
            #   - Linux: SO_REUSEADDR only permits rebinding past TIME_WAIT;
            #     disabling it would make a quick gateway restart fail to
            #     bind for up to ~60s — keep the default (enabled).
            self._site = web.TCPSite(
                self._runner,
                self.webhook_host,
                self.webhook_port,
                reuse_address=False if sys.platform == "darwin" else None,
            )
            await self._site.start()
        except OSError as exc:
            self._set_fatal_error(
                "bind_failed",
                "Could not bind LINE webhook on "
                f"{self.webhook_host or 'all IPv4+IPv6 interfaces'}:"
                f"{self.webhook_port}: {exc}",
                retryable=True,
            )
            return False

        self._mark_connected()
        logger.info(
            "LINE: webhook listening on %s:%s%s%s",
            self.webhook_host or "* (all interfaces, IPv4+IPv6)",
            self.webhook_port,
            self.webhook_path,
            f" (public: {self.public_base_url})" if self.public_base_url else "",
        )
        return True

    async def disconnect(self) -> None:
        self._mark_disconnected()

        if self._site is not None:
            try:
                await self._site.stop()
            except Exception:
                pass
            self._site = None
        if self._runner is not None:
            try:
                await self._runner.cleanup()
            except Exception:
                pass
            self._runner = None
        self._app = None

        # Cleanup any tracked tempfiles.
        for path in list(self._media_temp_paths):
            try:
                os.unlink(path)
            except OSError:
                pass
        self._media_temp_paths.clear()
        self._media_tokens.clear()

        if self._lock_key:
            try:
                from gateway.status import release_scoped_lock
                release_scoped_lock("line", self._lock_key)
            except Exception:
                pass
            self._lock_key = None

    # ------------------------------------------------------------------
    # Webhook handlers
    # ------------------------------------------------------------------

    async def _handle_health(self, request) -> Any:
        from aiohttp import web
        return web.json_response({"status": "ok", "platform": "line"})

    async def _handle_webhook(self, request) -> Any:
        from aiohttp import web

        # Body cap defends against memory-exhaustion via crafted Content-Length
        # (aiohttp's client_max_size only applies to certain body modes).
        try:
            body = await request.read()
        except Exception as exc:
            logger.debug("LINE: read failed: %s", exc)
            return web.Response(status=400, text="bad request")
        if len(body) > WEBHOOK_BODY_MAX_BYTES:
            return web.Response(status=413, text="payload too large")

        signature = request.headers.get("X-Line-Signature", "")
        if not verify_line_signature(body, signature, self.channel_secret):
            return web.Response(status=401, text="invalid signature")

        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return web.Response(status=400, text="bad json")

        events = payload.get("events", []) or []
        for event in events:
            try:
                await self._dispatch_event(event)
            except Exception:
                logger.exception("LINE: dispatch_event failed")

        return web.Response(status=200, text="ok")

    async def _dispatch_event(self, event: Dict[str, Any]) -> None:
        event_type = event.get("type")
        source = event.get("source") or {}
        webhook_event_id = event.get("webhookEventId", "") or ""

        # Dedup retries (LINE webhooks may be re-delivered).
        if webhook_event_id and self._dedup.is_duplicate(webhook_event_id):
            logger.debug("LINE: ignoring duplicate webhook event %s", webhook_event_id)
            return

        # Filter our own messages (self-echo).
        sender_user_id = source.get("userId", "")
        if self._bot_user_id and sender_user_id == self._bot_user_id:
            return

        authorized = self._source_authorized(source)

        if event_type == "message":
            # Message events carry the routing nuance (@mention gating,
            # unauthorized English reply, passive observe-recording), so the
            # authorization *decision* is made here but the *policy* is applied
            # inside the handler where replyToken / mention / text are known.
            await self._handle_message_event(event, authorized=authorized)
        elif event_type == "postback":
            # Postbacks only make sense from an already-authorized source
            # (the button was sent to them after a prior authorized turn).
            if not authorized:
                logger.info("LINE: rejecting postback from unauthorized %s", source)
                return
            await self._handle_postback_event(event)
        elif event_type in {"follow", "join"}:
            # New source reached the bot — notify admins once (dedup) so they
            # can approve it via the dashboard / approval tool.
            logger.info("LINE: lifecycle event %s from %s", event_type, source)
            if not authorized:
                await self._maybe_notify_new_source(source)
        elif event_type in {"unfollow", "leave"}:
            logger.info("LINE: lifecycle event %s from %s", event_type, source)
        else:
            logger.debug("LINE: ignoring event type %r", event_type)

    async def _handle_message_event(
        self, event: Dict[str, Any], *, authorized: bool = True
    ) -> None:
        msg = event.get("message") or {}
        msg_type = msg.get("type", "")
        message_id = msg.get("id", "")
        reply_token = event.get("replyToken", "")
        source = event.get("source") or {}
        chat_id, chat_type = _resolve_chat(source)
        user_id = source.get("userId", "") or chat_id

        # Stash the reply token for outbound use (reject replies use it too).
        if chat_id and reply_token:
            self._reply_tokens[chat_id] = (
                reply_token,
                time.time() + LINE_REPLY_TOKEN_TTL_SECONDS,
            )

        mentioned = _bot_mentioned(msg, self._bot_user_id)

        # Quote-reply of the bot's OWN message = implicit @mention: replying to
        # something Toothless said is clearly addressing it, so the user should
        # not have to type "@Toothless" again. LINE puts the quoted message's id
        # in ``quotedMessageId``; we only treat it as a mention when that id is
        # one WE sent (tracked in _LineClient.sent_message_ids) — quoting another
        # member's message does NOT count (stays observe-only).
        if not mentioned:
            qmid = (msg or {}).get("quotedMessageId")
            if qmid and self._client and str(qmid) in self._client.sent_message_ids:
                mentioned = True

        # ---- Authorization / routing policy (whitelist subsystem) ----------
        if chat_type == "dm":
            if not authorized:
                # Stranger DM: throttled English reply + notify admins (dedup)
                # + record the attempt. Never trigger the agent. (§2.7)
                await self._reject_unauthorized(
                    chat_id=chat_id, chat_type=chat_type, user_id=user_id,
                    reply_token=reply_token, attempt_text=_message_text(msg),
                    dm=True,
                )
                return
            # authorized DM → trigger (no @mention required)
        else:  # group / room
            if not authorized:
                # Unauthorized group: reply English only if the bot was @'d
                # (throttled) + notify admins (dedup). Non-@ stays silent, and
                # we do NOT observe-record content from a non-whitelisted
                # group. (§2.6 / §6.5)
                if mentioned:
                    await self._reject_unauthorized(
                        chat_id=chat_id, chat_type=chat_type, user_id=user_id,
                        reply_token=reply_token, attempt_text="", dm=False,
                    )
                return
            # authorized group/room
            requires_mention = self._group_requires_mention(chat_id)
            if requires_mention and self._bot_user_id is None:
                # SAFETY FAIL-OPEN: matching LINE mentionees requires our own
                # bot userId, fetched via GET /v2/bot/info at connect(). If that
                # failed, _bot_mentioned() can NEVER return True — enforcing the
                # mention gate here would silence every message in an authorized
                # group (over-correction). Fall back to pre-whitelist behaviour
                # (trigger the agent) and warn once so the operator can fix it.
                self._warn_mention_gate_unavailable()
            elif requires_mention and not mentioned:
                # Passive observe-record — record as context, do NOT trigger. (§6)
                await self._observe_record(
                    source=source, chat_id=chat_id, chat_type=chat_type,
                    user_id=user_id, msg=msg, msg_type=msg_type,
                    message_id=message_id,
                )
                return
            # mentioned / requires_mention disabled / gate unavailable → trigger

        # ---- Trigger path: media + build event + handle_message ------------
        # Handle media inbound — fetch the binary, cache it, and surface a
        # vision-tool-friendly local path on the MessageEvent.
        media_urls: List[str] = []
        media_types: List[str] = []
        text = ""

        if msg_type == "text":
            text = msg.get("text", "") or ""
        elif msg_type in ("image", "audio", "video", "file"):
            file_name = msg.get("fileName") or msg.get("file_name") or ""
            local_path, media_type = await self._download_media(
                message_id,
                msg_type,
                filename=file_name or None,
            )
            if local_path:
                media_urls.append(local_path)
                media_types.append(media_type)
            # Surface the real filename in the placeholder so the agent can
            # refer to the document naturally (e.g. "[file: receipt.pdf]").
            text = (
                f"[file: {file_name}]"
                if (msg_type == "file" and file_name)
                else f"[{msg_type}]"
            )
        elif msg_type == "sticker":
            keywords = msg.get("keywords") or []
            text = f"[sticker: {', '.join(keywords)}]" if keywords else "[sticker]"
        elif msg_type == "location":
            title = msg.get("title", "")
            address = msg.get("address", "")
            text = f"[location: {title} {address}]".strip()
        else:
            text = f"[unsupported message type: {msg_type}]"

        # On-demand media backfill (§6/§7): a triggering GROUP message that
        # carries no media of its own — e.g. "@Toothless what's the amount on
        # that receipt?" after silently uploading it — deterministically pulls in
        # the recently OBSERVED image/file uploads within the backfill window.
        # Images are vision-read (cached) and injected as channel_context text;
        # files are attached as media so the agent can extract them. Complements
        # quote-reply (explicit) for the "剛剛那張" case where the user didn't
        # quote. The program decides — the model never has to search. No media of
        # its own → only then.
        backfill_context: Optional[str] = None
        if not media_urls and chat_type in {"group", "room"}:
            backfill_context, bf_urls, bf_types = await self._backfill_recent_media(
                chat_id, chat_type
            )
            if bf_urls:
                media_urls.extend(bf_urls)
                media_types.extend(bf_types)

        # Quote reply (§8): if this message quotes an earlier one, look the
        # original up in the transcript and prepend it as context.
        quote_ctx = await self._quote_context(source, chat_id, chat_type, msg)
        if quote_ctx:
            text = f"{quote_ctx}\n\n{text}" if text else quote_ctx

        # Best-effort typing indicator (DM only).
        if chat_type == "dm" and self._client:
            asyncio.create_task(self._client.loading(chat_id))

        # Best-effort display-name resolution (falls back to raw IDs). §2.3
        user_name = await self._resolve_name(chat_id, chat_type, user_id) or user_id
        chat_name = await self._resolve_chat_name(chat_id, chat_type) or chat_id

        source_obj = self.build_source(
            chat_id=chat_id,
            chat_type=chat_type,
            user_id=user_id,
            user_name=user_name,
            chat_name=chat_name,
            message_id=message_id,
        )

        event_obj = MessageEvent(
            text=text,
            message_type=_LINE_MESSAGE_TYPES.get(msg_type, MessageType.TEXT),
            source=source_obj,
            raw_message=event,
            message_id=message_id,
            media_urls=media_urls,
            media_types=media_types,
            # Deterministic media-backfill content (vision text for recent group
            # images + recent-uploads hint); gateway prepends it to this turn.
            channel_context=backfill_context,
        )

        await self.handle_message(event_obj)

    # ------------------------------------------------------------------
    # Whitelist subsystem — authorization / observe / notify / naming
    # ------------------------------------------------------------------

    def _source_authorized(self, source: Dict[str, Any]) -> bool:
        """Authorized if the source matches the static env allowlists
        (backward-compat) OR the live config-backed whitelist store (which
        hot-reloads on every query)."""
        if _allowed_for_source(
            source,
            allow_all=self.allow_all,
            user_ids=self.allowed_users,
            group_ids=self.allowed_groups,
            room_ids=self.allowed_rooms,
        ):
            return True
        if self._whitelist is not None:
            sid, stype = _resolve_chat(source)
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
        """Warn (once per connection) that the @mention gate cannot be enforced
        because our own bot userId is unknown, so it has failed open."""
        if self._mention_gate_warned:
            return
        self._mention_gate_warned = True
        logger.warning(
            "LINE: requires_mention is enabled but the bot userId is unknown "
            "(GET /v2/bot/info failed at connect) — @mention detection is "
            "impossible, so the mention gate is DISABLED (fail-open): authorized "
            "groups will keep receiving replies WITHOUT an @mention. Restore "
            "connectivity to https://api.line.me/v2/bot/info (check "
            "LINE_CHANNEL_ACCESS_TOKEN) and reconnect to re-enable mention gating."
        )

    async def _send_plain(self, chat_id: str, reply_token: str, text: str) -> None:
        """Send one plain-text bubble via reply (preferred) or push fallback."""
        if not self._client or not text:
            return
        messages = [_text_message(text)]
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

    async def _notify_admin_unauthorized(
        self, chat_type: str, source_id: str, display: str = ""
    ) -> None:
        """Notify admins about an unauthorized/new source (deduped in helper)."""
        if self._whitelist is None:
            return
        gw = self._load_gateway_config()
        if gw is None:
            return
        try:
            try:
                from .whitelist_notify import notify_unauthorized
            except ImportError:  # pragma: no cover - standalone plugin-loader path
                from plugins.platforms.line.whitelist_notify import notify_unauthorized
            await notify_unauthorized(
                self._whitelist, gw,
                source_type=chat_type, source_id=source_id, display=display,
            )
        except Exception:
            logger.debug("LINE: notify_unauthorized failed", exc_info=True)

    async def _source_display_name(self, chat_id: str, chat_type: str, user_id: str) -> str:
        """Best-effort display name for a SOURCE (pending-queue attribution).

        A group/room source resolves to the group name (getGroupSummary); a DM
        source resolves to the sender's profile name. Falls back to '' so the
        caller can substitute the raw id.
        """
        if chat_type == "dm":
            return await self._resolve_name(chat_id, chat_type, user_id) or ""
        return await self._resolve_chat_name(chat_id, chat_type) or ""

    def _record_pending(self, source_id: str, source_type: str, name: str) -> None:
        """Log an unauthorized attempt into the pending queue (best-effort)."""
        if self._whitelist is None:
            return
        try:
            self._whitelist.record_attempt(
                source_id, platform="line", source_type=source_type, name=name,
            )
        except Exception:
            logger.debug("LINE: record_attempt failed", exc_info=True)

    async def _maybe_notify_new_source(self, source: Dict[str, Any]) -> None:
        sid, stype = _resolve_chat(source)
        if not sid:
            return
        name = await self._resolve_chat_name(sid, stype) or ""
        self._record_pending(sid, stype, name)
        await self._notify_admin_unauthorized(stype, sid, display=name or sid)

    async def _reject_unauthorized(
        self, *, chat_id: str, chat_type: str, user_id: str,
        reply_token: str, attempt_text: str, dm: bool,
    ) -> None:
        """Unauthorized-source handling: record the attempt into the pending
        queue (with resolved name), notify admins (dedup), send a throttled
        English reply."""
        name = await self._source_display_name(chat_id, chat_type, user_id)
        self._record_pending(chat_id, chat_type, name)
        await self._notify_admin_unauthorized(
            chat_type, chat_id, display=name or (user_id if dm else chat_id)
        )

        if self._whitelist is not None:
            try:
                if not self._whitelist.should_reply_unauthorized(chat_id):
                    return
            except Exception:
                pass
        await self._send_plain(
            chat_id, reply_token, UNAUTH_DM_REPLY if dm else UNAUTH_GROUP_REPLY
        )
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

    async def _resolve_name(
        self, chat_id: str, chat_type: str, user_id: str
    ) -> Optional[str]:
        """Display name of a user. DM → profile; group/room → member profile."""
        if not user_id or not self._client:
            return None
        ck = (
            f"u:{chat_id}:{user_id}"
            if chat_type in {"group", "room"}
            else f"u:{user_id}"
        )
        cached = self._name_cache_get(ck)
        if cached:
            return cached
        try:
            if chat_type == "dm":
                name = await self._client.get_profile(user_id)
            else:
                name = await self._client.get_member_name(
                    chat_id, user_id, chat_type=chat_type
                )
        except Exception:
            name = None
        self._name_cache_put(ck, name)
        return name

    async def _resolve_chat_name(
        self, chat_id: str, chat_type: str
    ) -> Optional[str]:
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

    # -- passive observe recording (§6) + quote reply (§8) ------------------

    async def _observe_record(
        self, *, source: Dict[str, Any], chat_id: str, chat_type: str,
        user_id: str, msg: Dict[str, Any], msg_type: str, message_id: str,
    ) -> None:
        """Record a message as passive observed context (no agent turn).

        Media policy (§7): drop video/audio entirely; record text. Images and
        files are recorded as a LIGHTWEIGHT ``[image]`` / ``[file: name]``
        placeholder together with their LINE ``platform_message_id`` — NO
        download or extraction here (cheap). When the bot is later triggered and
        the triggering message has no media of its own, ``_backfill_recent_media``
        re-fetches these recently-observed uploads within the backfill window and
        pulls them into that turn (on-demand, not per-message). The observed rows
        land in a single shared, chat-scoped session (per-user identity dropped).
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
        elif msg_type in {"image", "sticker", "location"}:
            body = f"[{msg_type}]"
        else:
            body = f"[{msg_type}]"
        if not body:
            return
        name = await self._resolve_name(chat_id, chat_type, user_id) or user_id
        content = f"[{name}|{user_id}]\n{body}"
        try:
            shared = self.build_source(
                chat_id=chat_id,
                chat_type=chat_type,
                chat_name=(await self._resolve_chat_name(chat_id, chat_type) or chat_id),
            )
            entry = store.get_or_create_session(shared)
            store.append_to_transcript(
                entry.session_id,
                {
                    "role": "user",
                    "content": content,
                    "observed": True,
                    "platform_message_id": message_id,
                },
            )
        except Exception:
            logger.debug("LINE: observe-record failed", exc_info=True)

    def _backfill_window_seconds(self) -> float:
        """Live backfill window in seconds — Dashboard-editable
        ``media_backfill_window_minutes`` (config, hot-reload), else the env/
        extra default. 0 disables backfill."""
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

    async def _bf_download(
        self, message_id: str, kind: str, file_name: str = "",
    ) -> Optional[Tuple[str, str]]:
        """Download-once memoization for backfill: reuse the cached file for a
        LINE message id as long as it still exists on disk, else re-fetch."""
        hit = self._bf_cache_get(self._bf_download_cache, message_id)
        if hit is not None:
            path, mime = hit
            if path and os.path.exists(path):
                return hit
        try:
            downloaded = await self._download_media(
                message_id, kind, file_name=file_name
            )
        except Exception:
            downloaded = None
        if downloaded:
            self._bf_cache_put(self._bf_download_cache, message_id, downloaded)
        return downloaded

    async def _bf_vision(self, message_id: str, path: str) -> Optional[str]:
        """Extract-once memoization: vision-read an image and cache the analysis
        text by the (immutable) LINE message id so re-pulling the same image
        into a later trigger turn never re-runs vision. §7 cost control."""
        cached = self._bf_cache_get(self._bf_vision_cache, message_id)
        if cached is not None:
            return cached
        try:
            from tools.vision_tools import vision_analyze_tool
            raw = await vision_analyze_tool(
                image_url=path,
                user_prompt=(
                    "請描述這張圖片的內容。若是收據、帳單、發票或菜單，"
                    "逐項列出商家名稱、各品項與金額、稅/服務費與總金額（含幣別）。"
                ),
            )
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
        """On-demand, deterministic media backfill (§6/§7). When the bot is
        triggered but the triggering message carries no media of its own, look
        back at recently-OBSERVED (unmentioned) image/file uploads in this group
        within the backfill window and make their content available to THIS turn
        — the program decides, the (weak) model never has to search.

        * **Images** are vision-read here (extract-once, cached by message id)
          and their analysis is injected as ``channel_context`` text — so we do
          NOT re-attach the raw image every turn (the gateway's vision pass has
          no cache and would re-bill it). This doubles as the auxiliary prompt
          hint listing recent uploads.
        * **Files/PDFs** are downloaded (download-once cache) and returned as
          ``media_urls`` so the agent can extract them with its file tools.

        Returns ``(channel_context, media_urls, media_types)``. Best-effort;
        never raises."""
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
                    hint_lines.append(
                        f"- 檔案 {label}（{hhmm} 上傳）：已附上本回合，可用檔案工具讀取"
                    )
            channel_context = None
            if hint_lines:
                channel_context = (
                    "[近期本群組上傳的媒體 / Recently uploaded media in this group]\n"
                    + "\n".join(hint_lines)
                )
                logger.info(
                    "LINE: backfilled %d recent observed media into trigger turn "
                    "(chat %s, window %.0fs, %d file attach)",
                    len(hint_lines), chat_id, window, len(urls),
                )
            return channel_context, urls, types
        except Exception:
            logger.debug("LINE: media backfill failed", exc_info=True)
            return None, [], []

    async def _quote_context(
        self, source: Dict[str, Any], chat_id: str, chat_type: str,
        msg: Dict[str, Any],
    ) -> Optional[str]:
        """If this message quotes an earlier one (``quotedMessageId``), look up
        the original in the transcript and return it as a context string.

        Best-effort: degrades to a short marker if the original can't be
        found (unrecorded, or aged out of retention). §8.
        """
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
                lookup_src = self.build_source(
                    chat_id=chat_id, chat_type=chat_type,
                    user_id=source.get("userId"),
                )
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

    async def _handle_postback_event(self, event: Dict[str, Any]) -> None:
        """User tapped the slow-LLM postback button — deliver cached payload."""
        postback = event.get("postback") or {}
        data = postback.get("data", "") or ""
        reply_token = event.get("replyToken", "")
        source = event.get("source") or {}
        chat_id, _ = _resolve_chat(source)

        try:
            parsed = json.loads(data)
        except (TypeError, json.JSONDecodeError):
            return

        if parsed.get("action") != "show_response":
            return
        request_id = parsed.get("request_id", "")
        if not request_id:
            return

        entry = self._cache.get(request_id)
        if not self._client or not reply_token or not entry:
            return

        if entry.state is State.READY:
            payload = entry.payload or ""
            chunks = split_for_line(strip_markdown_preserving_urls(str(payload)))
            messages = [_text_message(c) for c in chunks][:LINE_MAX_MESSAGES_PER_CALL]
            try:
                await self._client.reply(reply_token, messages)
                self._cache.mark_delivered(request_id)
                self._pending_buttons.pop(chat_id, None)
            except Exception as exc:
                logger.warning("LINE: postback reply failed (%s); falling back to push", exc)
                try:
                    await self._client.push(chat_id, messages)
                    self._cache.mark_delivered(request_id)
                    self._pending_buttons.pop(chat_id, None)
                except Exception as exc2:
                    logger.error("LINE: postback push fallback failed: %s", exc2)
        elif entry.state is State.ERROR:
            text = str(entry.payload or self.interrupted_text)
            try:
                await self._client.reply(reply_token, [_text_message(text)])
                self._cache.mark_delivered(request_id)
                self._pending_buttons.pop(chat_id, None)
            except Exception as exc:
                logger.warning("LINE: postback ERROR reply failed: %s", exc)
        elif entry.state is State.DELIVERED:
            try:
                await self._client.reply(reply_token, [_text_message(self.delivered_text)])
            except Exception:
                pass
        elif entry.state is State.PENDING:
            # Still working — re-issue the wait notice.
            try:
                await self._client.reply(reply_token, [_text_message(self.pending_text)])
            except Exception:
                pass

    async def _download_media(
        self,
        message_id: str,
        msg_type: str,
        *,
        filename: Optional[str] = None,
    ) -> Tuple[Optional[str], str]:
        if not self._client or not message_id:
            return None, ""
        try:
            data = await self._client.fetch_content(message_id)
        except Exception as exc:
            logger.warning("LINE: failed to fetch %s content for %s: %s", msg_type, message_id, exc)
            return None, ""
        ext = {
            "image": ".jpg",
            "audio": ".m4a",
            "video": ".mp4",
            "file": ".bin",
        }.get(msg_type, ".bin")
        try:
            if msg_type == "image":
                return cache_image_from_bytes(data, ext=ext), "image/jpeg"
            if msg_type == "audio":
                media_type = mimetypes.guess_type(f"audio{ext}")[0] or "audio/mp4"
                return cache_audio_from_bytes(data, ext=ext), media_type
            if msg_type == "video":
                media_type = mimetypes.guess_type(f"video{ext}")[0] or "video/mp4"
                return cache_video_from_bytes(data, ext=ext), media_type
            document_name = filename or f"line_file{ext}"
            return (
                cache_document_from_bytes(data, document_name),
                mimetypes.guess_type(document_name)[0] or "application/octet-stream",
            )
        except Exception as exc:
            logger.warning("LINE: failed to cache %s payload: %s", msg_type, exc)
            return None, ""

    # ------------------------------------------------------------------
    # Outbound send (text)
    # ------------------------------------------------------------------

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        if not self._client:
            return SendResult(success=False, error="LINE adapter not connected")

        # System busy-acks (interrupting / queued / steered) bypass the
        # postback cache and route directly to LINE so they reach the user
        # as visible bubbles. Source: PR #18153.
        if _is_system_bypass(content):
            return await self._send_text_chunks(chat_id, content, force_push=False)

        # If the chat has a PENDING postback button outstanding, route the
        # response into the cache for the user to fetch via tap.
        pending_rid = self._pending_buttons.get(chat_id)
        if pending_rid:
            self._cache.set_ready(pending_rid, content)
            return SendResult(success=True, message_id=pending_rid)

        return await self._send_text_chunks(chat_id, content, force_push=False)

    async def _send_text_chunks(
        self,
        chat_id: str,
        content: str,
        *,
        force_push: bool,
    ) -> SendResult:
        if not self._client:
            return SendResult(success=False, error="LINE adapter not connected")

        chunks = split_for_line(strip_markdown_preserving_urls(content))
        if not chunks:
            return SendResult(success=True, message_id=None)
        messages = [_text_message(c) for c in chunks][:LINE_MAX_MESSAGES_PER_CALL]

        token, used_reply = self._consume_reply_token(chat_id)
        if used_reply and not force_push:
            try:
                await self._client.reply(token, messages)
                return SendResult(success=True, message_id=token)
            except Exception as exc:
                logger.info("LINE: reply token rejected (%s); falling back to push", exc)
                # fall through to push

        try:
            await self._client.push(chat_id, messages)
            return SendResult(success=True, message_id=None)
        except Exception as exc:
            logger.error("LINE: push send failed: %s", exc)
            return SendResult(success=False, error=str(exc))

    def _consume_reply_token(self, chat_id: str) -> Tuple[str, bool]:
        """Consume a stashed reply token if present and unexpired.

        Returns ``(token, used_reply)``.
        """
        entry = self._reply_tokens.pop(chat_id, None)
        if not entry:
            return "", False
        token, expires_at = entry
        if not token or time.time() >= expires_at:
            return "", False
        return token, True

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        """Trigger LINE's loading-animation indicator (DM only)."""
        if self._client and chat_id:
            await self._client.loading(chat_id)

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        """Best-effort chat info derived from the chat_id prefix.

        LINE's chat-info APIs are limited and per-source-type — instead of
        chasing them we infer from the well-known ID prefixes:
        ``U`` = user (1:1), ``C`` = group, ``R`` = room. The agent only
        needs ``name`` + ``type`` from this method.
        """
        prefix = (chat_id or "")[:1]
        chat_type = {"U": "dm", "C": "group", "R": "channel"}.get(prefix, "dm")
        return {"name": chat_id or "", "type": chat_type}

    def format_message(self, content: str) -> str:
        """Strip Markdown that LINE can't render. URLs are preserved."""
        return strip_markdown_preserving_urls(content)

    # ------------------------------------------------------------------
    # Slow-LLM postback button — driven by _keep_typing
    # ------------------------------------------------------------------

    async def _keep_typing(self, chat_id: str, *args, **kwargs) -> None:
        """Override the base loop to fire the postback button at threshold.

        We intentionally keep the base implementation behind us: it's
        responsible for the typing-indicator heartbeat, while *this*
        wrapper layers in the slow-LLM postback bubble at threshold.
        """
        if (
            self.slow_response_threshold <= 0
            or not self._client
            or not chat_id
        ):
            await super()._keep_typing(chat_id, *args, **kwargs)
            return

        async def _fire_postback() -> None:
            try:
                await asyncio.sleep(self.slow_response_threshold)
            except asyncio.CancelledError:
                raise
            # Only fire if we still have a usable reply token. If the agent
            # already responded, _consume_reply_token has cleared it.
            if chat_id not in self._reply_tokens:
                return
            if chat_id in self._pending_buttons:
                return
            rid = self._cache.register_pending(chat_id)
            self._pending_buttons[chat_id] = rid
            token, used = self._consume_reply_token(chat_id)
            if not used:
                self._pending_buttons.pop(chat_id, None)
                return
            msg = build_postback_button_message(
                self.pending_text, self.button_label, rid
            )
            try:
                await self._client.reply(token, [msg])
                logger.info("LINE: sent slow-LLM postback button for chat %s (rid=%s)", chat_id, rid)
            except Exception as exc:
                logger.warning("LINE: postback button send failed: %s", exc)
                self._pending_buttons.pop(chat_id, None)

        post_task = asyncio.create_task(_fire_postback())
        try:
            await super()._keep_typing(chat_id, *args, **kwargs)
        finally:
            if not post_task.done():
                post_task.cancel()
                try:
                    await post_task
                except (asyncio.CancelledError, Exception):
                    pass

    async def interrupt_session_activity(self, session_key: str, chat_id: str) -> None:
        """Resolve any orphan PENDING postback so the button doesn't loop."""
        await super().interrupt_session_activity(session_key, chat_id)
        rid = self._pending_buttons.pop(chat_id, None)
        if rid:
            self._cache.set_error(rid, self.interrupted_text)

    # ------------------------------------------------------------------
    # Outbound media (image / voice / video)
    # ------------------------------------------------------------------

    def _register_media(self, file_path: str, *, cleanup: bool = False) -> str:
        """Register a local file for HTTPS serving; return the URL token."""
        # Evict expired tokens first.
        now = time.time()
        for token in list(self._media_tokens.keys()):
            path, exp = self._media_tokens[token]
            if now > exp:
                self._media_tokens.pop(token, None)
                if path in self._media_temp_paths:
                    self._media_temp_paths.discard(path)
                    try:
                        os.unlink(path)
                    except OSError:
                        pass

        resolved = str(Path(file_path).resolve())
        token = secrets.token_urlsafe(32)
        self._media_tokens[token] = (resolved, now + self._media_ttl)
        if cleanup:
            self._media_temp_paths.add(resolved)
        return token

    def _media_url(self, token: str, filename: str) -> str:
        """Build the public HTTPS URL for a media token. PR #8398 style."""
        if self.public_base_url:
            base = self.public_base_url
        else:
            # A wildcard/dual-stack bind has no fetchable hostname; the
            # _missing_public_url guard should have caught this earlier.
            # Fall back to localhost so the URL is at least well-formed.
            host = self.webhook_host
            if host is None or host in _WILDCARD_HOSTS:
                host = "127.0.0.1"
            port = self.webhook_port
            if port == 443:
                base = f"https://{host}"
            else:
                base = f"https://{host}:{port}"
        safe_name = _urlquote(filename, safe="")
        return f"{base}{DEFAULT_MEDIA_PATH_PREFIX}/{token}/{safe_name}"

    def _missing_public_url(self) -> bool:
        """True when outbound media cannot work: no LINE_PUBLIC_URL and the
        bind host is a wildcard (or the dual-stack ``None`` default), i.e.
        not an address LINE's fetchers could ever reach."""
        if self.public_base_url:
            return False
        return self.webhook_host is None or self.webhook_host in _WILDCARD_HOSTS

    async def _handle_media(self, request) -> Any:
        """Serve a registered local file over HTTPS for LINE's media URLs.

        Defence-in-depth: even though ``_register_media`` is only called
        from trusted internal code, we recheck the resolved path against
        an allowed-roots set before serving. Sources allowed:
        ``tempfile.gettempdir()``, ``/tmp`` (which resolves to
        ``/private/tmp`` on macOS), and ``HERMES_HOME``. PR #8398.
        """
        from aiohttp import web

        token = request.match_info["token"]
        entry = self._media_tokens.get(token)
        if not entry:
            return web.Response(status=404, text="not found")

        file_path, expires_at = entry
        if time.time() > expires_at:
            self._media_tokens.pop(token, None)
            return web.Response(status=410, text="gone")

        path = Path(file_path)
        if not path.exists() or not path.is_file():
            return web.Response(status=404, text="not found")

        try:
            from hermes_constants import get_hermes_home
            hermes_home = Path(get_hermes_home()).resolve()
        except Exception:
            hermes_home = Path.home().joinpath(".hermes").resolve()

        allowed_roots = {
            Path(tempfile.gettempdir()).resolve(),
            Path("/tmp").resolve(),  # → /private/tmp on macOS
            hermes_home,
        }
        resolved = path.resolve()
        if not any(_is_relative_to(resolved, r) for r in allowed_roots):
            logger.warning("LINE: refusing to serve outside allowed roots: %s", resolved)
            return web.Response(status=403, text="forbidden")

        content_type, _ = mimetypes.guess_type(str(path))
        return web.FileResponse(
            path,
            headers={"Content-Type": content_type or "application/octet-stream"},
        )

    async def send_image_file(
        self,
        chat_id: str,
        image_path: str,
        caption: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        path = Path(image_path)
        if not path.exists() or not path.is_file():
            return SendResult(success=False, error=f"image file not found: {image_path}")
        if path.stat().st_size > LINE_IMAGE_MAX_BYTES:
            return SendResult(success=False, error="image exceeds 10 MB LINE limit")
        if not self._client:
            return SendResult(success=False, error="LINE adapter not connected")
        if self._missing_public_url():
            return SendResult(
                success=False,
                error="LINE_PUBLIC_URL must be set to send images "
                "(LINE only accepts publicly reachable HTTPS URLs)",
            )

        token = self._register_media(str(path.resolve()))
        url = self._media_url(token, path.name)
        if not url.lower().startswith("https://"):
            return SendResult(success=False, error=f"LINE image URL must be HTTPS: {url}")
        msgs: List[Dict[str, Any]] = [_image_message(url)]
        if caption:
            msgs.append(_text_message(caption))
        return await self._send_messages(chat_id, msgs)

    async def send_voice(
        self,
        chat_id: str,
        audio_path: str,
        duration_ms: int = 1000,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        path = Path(audio_path)
        if not path.exists() or not path.is_file():
            return SendResult(success=False, error=f"audio file not found: {audio_path}")
        if path.stat().st_size > LINE_AV_MAX_BYTES:
            return SendResult(success=False, error="audio exceeds 200 MB LINE limit")
        if not self._client:
            return SendResult(success=False, error="LINE adapter not connected")
        if self._missing_public_url():
            return SendResult(
                success=False,
                error="LINE_PUBLIC_URL must be set to send audio",
            )

        token = self._register_media(str(path.resolve()))
        url = self._media_url(token, path.name)
        return await self._send_messages(chat_id, [_audio_message(url, duration_ms)])

    async def send_video(
        self,
        chat_id: str,
        video_path: str,
        preview_path: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        path = Path(video_path)
        if not path.exists() or not path.is_file():
            return SendResult(success=False, error=f"video file not found: {video_path}")
        if path.stat().st_size > LINE_AV_MAX_BYTES:
            return SendResult(success=False, error="video exceeds 200 MB LINE limit")
        if not self._client:
            return SendResult(success=False, error="LINE adapter not connected")
        if self._missing_public_url():
            return SendResult(
                success=False,
                error="LINE_PUBLIC_URL must be set to send video",
            )

        # LINE requires a previewImageUrl. Use one if supplied, otherwise
        # write a stdlib 1×1 PNG to /tmp and serve it. PR #8398.
        if preview_path and Path(preview_path).is_file():
            preview_token = self._register_media(str(Path(preview_path).resolve()))
            preview_filename = Path(preview_path).name
        else:
            tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
            try:
                tmp.write(_FALLBACK_PNG_PREVIEW)
                tmp.flush()
                tmp.close()
                preview_token = self._register_media(tmp.name, cleanup=True)
                preview_filename = "preview.png"
            except Exception:
                try:
                    os.unlink(tmp.name)
                except OSError:
                    pass
                raise

        video_token = self._register_media(str(path.resolve()))
        video_url = self._media_url(video_token, path.name)
        preview_url = self._media_url(preview_token, preview_filename)
        return await self._send_messages(chat_id, [_video_message(video_url, preview_url)])

    async def _send_messages(
        self,
        chat_id: str,
        messages: List[Dict[str, Any]],
    ) -> SendResult:
        """Send already-built message objects, batched at 5/call."""
        if not self._client:
            return SendResult(success=False, error="LINE adapter not connected")
        if not messages:
            return SendResult(success=True, message_id=None)

        first_batch = messages[:LINE_MAX_MESSAGES_PER_CALL]
        rest = messages[LINE_MAX_MESSAGES_PER_CALL:]

        # First batch: try reply token, fall back to push.
        token, used_reply = self._consume_reply_token(chat_id)
        if used_reply:
            try:
                await self._client.reply(token, first_batch)
            except Exception as exc:
                logger.info("LINE: reply token rejected (%s); falling back to push", exc)
                try:
                    await self._client.push(chat_id, first_batch)
                except Exception as exc2:
                    return SendResult(success=False, error=str(exc2))
        else:
            try:
                await self._client.push(chat_id, first_batch)
            except Exception as exc:
                return SendResult(success=False, error=str(exc))

        # Subsequent batches: always push (reply token is single-use).
        while rest:
            batch = rest[:LINE_MAX_MESSAGES_PER_CALL]
            rest = rest[LINE_MAX_MESSAGES_PER_CALL:]
            try:
                await self._client.push(chat_id, batch)
            except Exception as exc:
                logger.warning("LINE: push for follow-up batch failed: %s", exc)
                return SendResult(success=False, error=str(exc))

        return SendResult(success=True, message_id=None)


def _is_relative_to(child: Path, parent: Path) -> bool:
    """Backport for Path.is_relative_to (Python 3.9+) — defensive against
    cwd-resolution differences across CI runners."""
    try:
        return child.resolve().is_relative_to(parent.resolve())
    except (AttributeError, ValueError):
        try:
            child.resolve().relative_to(parent.resolve())
            return True
        except ValueError:
            return False


# ---------------------------------------------------------------------------
# Plugin entry-point hooks
# ---------------------------------------------------------------------------

def check_requirements() -> bool:
    """Plugin gate: require credentials AND aiohttp at runtime."""
    if not _get_scoped_secret("LINE_CHANNEL_ACCESS_TOKEN"):
        return False
    if not _get_scoped_secret("LINE_CHANNEL_SECRET"):
        return False
    try:
        import aiohttp  # noqa: F401
    except ImportError:
        return False
    return True


def validate_config(config) -> bool:
    extra = getattr(config, "extra", {}) or {}
    has_token = bool(
        _get_scoped_secret("LINE_CHANNEL_ACCESS_TOKEN") or extra.get("channel_access_token")
    )
    has_secret = bool(
        _get_scoped_secret("LINE_CHANNEL_SECRET") or extra.get("channel_secret")
    )
    return has_token and has_secret


def is_connected(config) -> bool:
    """Surface in ``hermes status`` even before the adapter is instantiated."""
    return validate_config(config)


def _env_enablement() -> Optional[Dict[str, Any]]:
    """Auto-seed PlatformConfig.extra from env-only setups.

    Lets ``hermes status`` reflect a LINE configuration that lives entirely
    in ``.env`` without a ``platforms.line`` block in ``config.yaml``.
    Mirrors the IRC plugin's pattern.
    """
    if not (_get_scoped_secret("LINE_CHANNEL_ACCESS_TOKEN") and _get_scoped_secret("LINE_CHANNEL_SECRET")):
        return None
    seeded: Dict[str, Any] = {}
    if os.getenv("LINE_PORT"):
        try:
            seeded["port"] = int(os.environ["LINE_PORT"])
        except ValueError:
            pass
    if os.getenv("LINE_HOST"):
        seeded["host"] = os.environ["LINE_HOST"]
    if os.getenv("LINE_PUBLIC_URL"):
        seeded["public_url"] = os.environ["LINE_PUBLIC_URL"]
    if os.getenv("LINE_HOME_CHANNEL"):
        seeded["home_channel"] = os.environ["LINE_HOME_CHANNEL"]
    return seeded or {}


async def _standalone_send(
    pconfig,
    chat_id: str,
    message: str,
    *,
    thread_id: Optional[str] = None,
    media_files: Optional[List[str]] = None,
    force_document: bool = False,
) -> Dict[str, Any]:
    """Out-of-process push delivery for cron jobs running detached from the gateway.

    Without this hook ``deliver=line`` cron jobs fail with ``no live adapter``
    when cron runs as its own process. We always Push (reply tokens require
    an inbound webhook event we don't have in this path).

    ``thread_id`` is accepted for signature parity but ignored — LINE has
    no native thread primitive on the channel-side API. ``media_files``
    likewise: cron-side media delivery requires a publicly-reachable URL,
    which the standalone path can't construct without binding the webhook
    server, so we send a text reference instead.
    """
    extra = getattr(pconfig, "extra", {}) or {}
    token = (
        _get_scoped_secret("LINE_CHANNEL_ACCESS_TOKEN")
        or extra.get("channel_access_token", "")
    )
    if not token or not chat_id:
        return {"error": "LINE standalone send: missing token or chat_id"}

    plain = strip_markdown_preserving_urls(message or "")
    chunks = split_for_line(plain) or [""]
    messages = [_text_message(c) for c in chunks][:LINE_MAX_MESSAGES_PER_CALL]
    if media_files:
        # Tack on a hint so the recipient knows media was generated but not delivered.
        messages.append(_text_message(f"[{len(media_files)} attachment(s) generated; not deliverable from cron]"))
        messages = messages[:LINE_MAX_MESSAGES_PER_CALL]

    client = _LineClient(token)
    try:
        await client.push(chat_id, messages)
        return {"success": True, "message_id": None}
    except Exception as exc:
        return {"error": str(exc)}


def interactive_setup() -> None:
    """Minimal stdin wizard for ``hermes setup line``.

    Mirrors the irc/teams style: prompts for the two required vars, plus
    one optional public URL. Writes to ``~/.hermes/.env`` via ``hermes_cli.config``.
    """
    print()
    print("LINE Messaging API setup")
    print("------------------------")
    print("Create a Messaging API channel at https://developers.line.biz/console/")
    print("then copy the values below.")
    print()

    try:
        from hermes_cli.config import get_env_var, set_env_var
    except ImportError:
        print("hermes_cli.config not available; set LINE_* vars manually in ~/.hermes/.env")
        return

    def _prompt(var: str, prompt: str, *, secret: bool = False) -> None:
        existing = get_env_var(var) if callable(get_env_var) else None
        suffix = " [keep current]" if existing else ""
        try:
            if secret:
                from hermes_cli.secret_prompt import masked_secret_prompt
                value = masked_secret_prompt(f"{prompt}{suffix}: ")
            else:
                value = input(f"{prompt}{suffix}: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if value:
            set_env_var(var, value)

    _prompt("LINE_CHANNEL_ACCESS_TOKEN", "Channel access token", secret=True)
    _prompt("LINE_CHANNEL_SECRET", "Channel secret", secret=True)
    _prompt("LINE_PUBLIC_URL", "Public HTTPS base URL (optional, e.g. https://my-tunnel.example.com)")
    _prompt("LINE_ALLOWED_USERS", "Allowed user IDs (comma-separated; blank=skip)")
    print("Done. Set the webhook URL in the LINE console to "
          "<your-public-url>/line/webhook and enable 'Use webhook'.")


def register(ctx) -> None:
    """Plugin entry point — called by the Hermes plugin system at startup."""
    ctx.register_platform(
        name="line",
        label="LINE",
        adapter_factory=lambda cfg: LineAdapter(cfg),
        check_fn=check_requirements,
        validate_config=validate_config,
        is_connected=is_connected,
        required_env=["LINE_CHANNEL_ACCESS_TOKEN", "LINE_CHANNEL_SECRET"],
        install_hint="pip install aiohttp",
        setup_fn=interactive_setup,
        env_enablement_fn=_env_enablement,
        cron_deliver_env_var="LINE_HOME_CHANNEL",
        standalone_sender_fn=_standalone_send,
        allowed_users_env="LINE_ALLOWED_USERS",
        allow_all_env="LINE_ALLOW_ALL_USERS",
        # LINE per-bubble cap is 5000; smart-chunker uses 4500.
        max_message_length=LINE_SAFE_BUBBLE_CHARS,
        emoji="💚",
        pii_safe=False,
        allow_update_command=True,
        platform_hint=(
            "You are chatting via LINE Messaging API. LINE does NOT render "
            "Markdown — text bubbles show ** and # literally. Bare URLs are "
            "auto-linked, but \\[label\\](url) syntax is not. Each text bubble "
            "is capped at 5000 characters and at most 5 bubbles are sent per "
            "reply, so keep responses concise. Image/audio/video sending "
            "requires LINE_PUBLIC_URL configured to a publicly reachable HTTPS "
            "host. Slow responses surface a 'Get answer' button the user taps "
            "to fetch the reply via a fresh free token."
        ),
    )
