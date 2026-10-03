"""Telegram platform override — entry point with a stock-adapter fallback.

The override subclasses the bundled adapter, so an upstream change to an internal seam can make
``adapter.py`` fail to import or its registration raise. Shadowing a bundled platform leaves no
automatic fallback, so this module provides it: on failure the bundled Telegram adapter is
registered instead and the error is logged. The platform then runs WITHOUT the LINE whitelist
decision card.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

PLATFORM = "telegram"
STOCK_MODULE = "plugins.platforms.telegram.adapter"

try:
    from . import adapter  # noqa: F401
    IMPORT_ERROR: Exception | None = None
except Exception as exc:
    adapter = None  # type: ignore[assignment]
    IMPORT_ERROR = exc


def _register_stock(ctx, reason: str) -> None:
    import importlib

    logger.error(
        "platforms/%s override unavailable (%s); registering the STOCK %s adapter instead — "
        "whitelist card is OFF until the override is fixed",
        PLATFORM, reason, PLATFORM,
    )
    importlib.import_module(STOCK_MODULE).register(ctx)


def register(ctx) -> None:
    if adapter is None:
        _register_stock(ctx, f"import failed: {IMPORT_ERROR!r}")
        return
    try:
        adapter.register(ctx)
    except Exception as exc:
        logger.exception("platforms/%s override registration raised", PLATFORM)
        _register_stock(ctx, f"registration raised: {exc!r}")


__all__ = ["register", "adapter", "IMPORT_ERROR"]
