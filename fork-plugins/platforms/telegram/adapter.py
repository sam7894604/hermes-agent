"""Telegram platform override — the LINE whitelist decision card.

Installed at ``$HERMES_HOME/plugins/platforms/telegram/`` it shadows the bundled ``platforms/telegram``
plugin. ``TelegramAdapter`` subclasses upstream's ``plugins.platforms.telegram.adapter`` and adds one
inline-keyboard flow; ``register()`` re-runs upstream's registration with this adapter so the platform
entry stays upstream's.

``send_whitelist_decision`` posts an Approve/Ignore/Skip keyboard whose ``linewl:`` callbacks are
dispatched ahead of upstream's ``_handle_callback_query`` table and drive the LINE override plugin's
``WhitelistStore``. The LINE plugin's ``notify_unauthorized`` picks this card when the notify target is
``telegram:<chat>``.
"""
from __future__ import annotations

import importlib.util
import logging
import sys
from typing import Any, Dict, Optional

from gateway.platforms.base import SendResult
from plugins.platforms.telegram import adapter as _base

logger = logging.getLogger(__name__)


def _whitelist_store_class():
    """``WhitelistStore`` from the LINE override plugin: the module the plugin manager already loaded when
    the LINE platform is active in this process (any profile scope), else a path import from the installed
    plugin directory. Raises when neither is available."""
    for name, mod in list(sys.modules.items()):
        if name.startswith("hermes_plugins.platforms__line") and name.endswith(".whitelist_store") and mod is not None:
            cls = getattr(mod, "WhitelistStore", None)
            if cls is not None:
                return cls
    cached = sys.modules.get("hermes_fork_line_whitelist_store")
    if cached is not None:
        return cached.WhitelistStore
    from hermes_constants import get_hermes_home
    path = get_hermes_home() / "plugins" / "platforms" / "line" / "whitelist_store.py"
    spec = importlib.util.spec_from_file_location("hermes_fork_line_whitelist_store", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"LINE whitelist store not installed at {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    sys.modules["hermes_fork_line_whitelist_store"] = mod
    return mod.WhitelistStore


class TelegramAdapter(_base.TelegramAdapter):
    """Upstream's adapter plus the whitelist decision card and its ``linewl:`` callbacks."""

    async def send_whitelist_decision(
        self, chat_id: str, source_type: str, source_id: str, name: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Interactive LINE whitelist-decision card (Approve/Ignore/Skip).

        ``callback_data`` stays well under Telegram's 64-byte limit: ``linewl:<action>:<source_type>:
        <source_id>`` — LINE ids are ~33 ascii chars and contain no colons, so the id is always the final
        field."""
        if not self._bot:
            return SendResult(success=False, error="Not connected")
        try:
            html = _base._html
            who = name or source_id
            text = (
                "🔔 <b>LINE: unauthorized access attempt</b>\n\n"
                f"type: {html.escape(str(source_type))}\n"
                f"id: <code>{html.escape(str(source_id))}</code>\n"
                f"name: {html.escape(str(who))}"
            )
            keyboard = _base.InlineKeyboardMarkup([[
                _base.InlineKeyboardButton("✅ Approve", callback_data=f"linewl:approve:{source_type}:{source_id}"),
                _base.InlineKeyboardButton("⛔ Ignore", callback_data=f"linewl:ignore:{source_type}:{source_id}"),
                _base.InlineKeyboardButton("➖ Skip", callback_data=f"linewl:skip:{source_type}:{source_id}"),
            ]])

            thread_id = self._metadata_thread_id(metadata)
            kwargs: Dict[str, Any] = {
                "chat_id": _base.normalize_telegram_chat_id(chat_id), "text": text,
                "parse_mode": _base.ParseMode.HTML, "reply_markup": keyboard, **self._link_preview_kwargs(),
            }
            reply_to_id = self._reply_to_message_id_for_send(None, metadata, reply_to_mode=self._reply_to_mode)
            kwargs["reply_to_message_id"] = reply_to_id
            kwargs.update(self._thread_kwargs_for_send(
                chat_id, thread_id, metadata, reply_to_message_id=reply_to_id, reply_to_mode=self._reply_to_mode))
            msg = await self._send_message_with_thread_fallback(**kwargs)
            return SendResult(success=True, message_id=str(msg.message_id))
        except Exception as e:
            logger.warning("[%s] send_whitelist_decision failed: %s", self.name, e)
            return SendResult(success=False, error=str(e))

    async def _handle_callback_query(self, update, context) -> None:
        """``linewl:`` buttons are ours; everything else goes to upstream's prefix table."""
        query = getattr(update, "callback_query", None)
        data = getattr(query, "data", None) if query is not None else None
        if isinstance(data, str) and data.startswith("linewl:"):
            self._accept_update()
            await self._handle_line_whitelist_callback(query, data, self._callback_ctx(query))
            return
        await super()._handle_callback_query(update, context)

    async def _handle_line_whitelist_callback(self, query, data: str, cb: Dict[str, Any]) -> None:
        """``linewl:action:source_type:id`` — approve/ignore hit the shared store; skip is a no-op. Every
        store touch is guarded so a tap can never crash the Telegram receive path."""
        parts = data.split(":", 3)
        if len(parts) != 4:
            await query.answer(text="Invalid whitelist data.")
            return
        _, action, source_type, source_id = parts

        caller_id = str(getattr(query.from_user, "id", ""))
        # Base gate: the adapter's normal callback auth (same as every other button); then additionally
        # require whitelist-admin so only admins may mutate the whitelist.
        if not await self._callback_authorized(query, cb, "⛔ You are not authorized."):
            return

        try:
            store = _whitelist_store_class()()
        except Exception:
            store = None

        if store is not None:
            try:
                # Cross-platform admin: a LINE whitelist admin OR the Telegram recipient of the notify card
                # (whose Telegram id won't match the LINE admin list).
                checker = getattr(store, "is_card_admin", None)
                _is_admin = checker("telegram", caller_id) if checker else store.is_admin(caller_id)
                if not _is_admin:
                    await query.answer(text="⛔ You are not a whitelist admin.")
                    return
            except Exception:
                # Admin check unavailable — fail closed on mutations.
                if action in ("approve", "ignore"):
                    await query.answer(text="Whitelist unavailable.")
                    return

        user_display = getattr(query.from_user, "first_name", "User")
        outcome = None
        try:
            if action == "approve":
                if store is None:
                    await query.answer(text="Whitelist unavailable.")
                    return
                res = store.approve_pending(source_id, added_by=caller_id)
                if isinstance(res, dict) and res.get("approved"):
                    scope = res.get("scope")
                    outcome = f"✅ Approved{f' ({scope})' if scope else ''} by {user_display}"
                else:
                    reason = res.get("reason") or "" if isinstance(res, dict) else ""
                    outcome = f"⚠️ Not approved{f': {reason}' if reason else ''}"
            elif action == "ignore":
                if store is None:
                    await query.answer(text="Whitelist unavailable.")
                    return
                ok = store.ignore_pending(source_id)
                outcome = f"⛔ Ignored by {user_display}" if ok else "⚠️ Nothing to ignore"
            elif action == "skip":
                outcome = f"➖ Skipped by {user_display}"
            else:
                await query.answer(text="Unknown action.")
                return
        except Exception as exc:
            logger.error("Telegram whitelist decision failed (action=%s id=%s): %s", action, source_id, exc)
            await query.answer(text="Whitelist action failed.")
            return

        await query.answer(text=outcome or "Done")
        try:
            await query.edit_message_text(
                text=self.format_message(outcome or "Done"), parse_mode=_base.ParseMode.MARKDOWN_V2,
                reply_markup=None)
        except Exception:
            pass  # non-fatal if the edit fails


def _build_adapter(config):
    """Mirror of upstream's factory (``TelegramAdapter`` + notification mode) for this subclass."""
    adapter = TelegramAdapter(config)
    try:
        adapter._notifications_mode = _base._resolve_notifications_mode()
    except Exception:
        adapter._notifications_mode = "important"
    return adapter


class _OverrideContext:
    """Delegate every registration to the real ``PluginContext``; only ``register_platform`` is
    intercepted, to swap the adapter factory, so upstream's kwargs are reused verbatim."""

    def __init__(self, ctx, **overrides: Any) -> None:
        self._ctx = ctx
        self._overrides = overrides

    def register_platform(self, *args: Any, **kwargs: Any):
        kwargs.update(self._overrides)
        return self._ctx.register_platform(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._ctx, name)


def register(ctx) -> None:
    """Plugin entry point: upstream's registration with this adapter."""
    _base.register(_OverrideContext(ctx, adapter_factory=_build_adapter))
