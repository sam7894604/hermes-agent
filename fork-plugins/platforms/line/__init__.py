"""LINE platform override — entry point with a stock-adapter fallback.

The override subclasses the bundled adapter, so an upstream change to an internal seam can make
``adapter.py`` fail to import or its registration raise. Shadowing a bundled platform leaves no
automatic fallback (``resolve_manifest_winners`` only knows one winner), so this module provides
it: on failure the bundled LINE adapter is registered instead and the error is logged. The
platform then runs WITHOUT the whitelist, @mention gate, passive context, media backfill, display
names and the ``line_whitelist`` tool — authorization falls back to the static ``LINE_ALLOWED_*``
env allowlists (empty allowlists deny everyone).
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

PLATFORM = "line"
STOCK_MODULE = "plugins.platforms.line.adapter"

try:
    from . import adapter  # noqa: F401  (bound as the package attribute tests and tooling read)
    IMPORT_ERROR: Exception | None = None
except Exception as exc:  # the override no longer fits the installed Hermes
    adapter = None  # type: ignore[assignment]
    IMPORT_ERROR = exc


def _register_stock(ctx, reason: str) -> None:
    import importlib

    logger.error(
        "platforms/%s override unavailable (%s); registering the STOCK %s adapter instead — "
        "whitelist / mention gate / observe / line_whitelist tool are OFF until the override is fixed",
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
