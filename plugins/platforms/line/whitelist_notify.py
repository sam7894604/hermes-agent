"""LINE whitelist — unauthorized-source notification + dedup helper (Phase 1).

The adapter calls :func:`notify_unauthorized` when a message arrives from a
source that is not on the whitelist. This helper owns the "notify admins at
most once per source" policy (delegated to
:meth:`WhitelistStore.should_notify_unauthorized`) and the actual delivery via
the shared ``_send_to_platform`` sender.

It is intentionally **defensive**: any failure (missing config, sender import
error, network) is swallowed and logged — an unauthorized-source event must
never crash the adapter's receive path. It returns ``True`` only when a
notification was actually dispatched.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from .whitelist_store import WhitelistStore

logger = logging.getLogger(__name__)

__all__ = ["notify_unauthorized"]


def _resolve_notify_target(store: WhitelistStore, config: Any) -> Optional[str]:
    """Resolve the admin chat_id to notify.

    Priority: ``platforms.line.unauthorized_notify`` (explicit override) →
    ``config.get_home_channel("line").chat_id`` → None.
    """
    # explicit override on the line config
    try:
        line_cfg = store._line_config()  # internal, but same-package helper
        override = line_cfg.get("unauthorized_notify")
        if override:
            return str(override)
    except Exception:
        pass

    # home channel
    try:
        from gateway.config import Platform

        home = config.get_home_channel(Platform("line"))
        if home is not None and getattr(home, "chat_id", None):
            return str(home.chat_id)
    except Exception:
        logger.debug("notify_unauthorized: home-channel resolution failed", exc_info=True)
    return None


def _resolve_line_pconfig(config: Any) -> Any:
    """Best-effort fetch of the LINE PlatformConfig object for the sender."""
    try:
        from gateway.config import Platform

        platforms = getattr(config, "platforms", None)
        if isinstance(platforms, dict):
            return platforms.get(Platform("line"))
    except Exception:
        logger.debug("notify_unauthorized: pconfig resolution failed", exc_info=True)
    return None


async def notify_unauthorized(
    store: WhitelistStore,
    config: Any,
    *,
    source_type: str,
    source_id: str,
    display: str = "",
) -> bool:
    """Notify admins about an unauthorized/new LINE source, at most once.

    Returns ``True`` iff a notification was actually sent. Never raises.
    """
    try:
        if not store.should_notify_unauthorized(source_id):
            return False

        target = _resolve_notify_target(store, config)
        if not target:
            logger.warning(
                "notify_unauthorized: no notify target for %s %s; skipping",
                source_type,
                source_id,
            )
            return False

        who = display or source_id
        message = (
            "🔔 LINE: unauthorized access attempt\n"
            f"type: {source_type}\n"
            f"id: {source_id}\n"
            f"name: {who}\n"
            "Use the approval tool / dashboard to allow or ignore."
        )

        # Lazy import to avoid pulling the heavy send stack at module load.
        from tools.send_message_tool import _send_to_platform
        from gateway.config import Platform

        pconfig = _resolve_line_pconfig(config)

        await _send_to_platform(
            Platform("line"),
            pconfig,
            target,
            message,
        )

        store.mark_unauthorized_notified(source_id)
        return True
    except Exception:
        logger.exception(
            "notify_unauthorized failed for %s %s", source_type, source_id
        )
        return False
