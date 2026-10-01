"""turbovault-fixups — deterministic argument repair for turbovault's ``edit_note``.

Weak models often emit ``SEARCH:``/``REPLACE:`` labels or ``old_string``/``new_string`` JSON
instead of aider SEARCH/REPLACE blocks, which the tool rejects ("No SEARCH/REPLACE blocks found
in input") and the agent then falls back to a full-note overwrite on every edit. The hook rewrites
the known malformed shapes before dispatch; anything unrecognized passes through untouched so
turbovault stays the final authority on what is valid.

A ``pre_tool_call`` plugin hook instead of core code: MCP tools reach the same hook as built-ins
under their registry name (``mcp__<server>__<tool>``), and ``{"action": "modify"}`` is the
documented way to rewrite arguments (website/docs/user-guide/features/hooks.md).
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from .normalize import normalize_edits

logger = logging.getLogger(__name__)

EDIT_NOTE_TOOL = "mcp__turbovault__edit_note"


def _on_pre_tool_call(tool_name: str = "", args: Optional[Dict[str, Any]] = None, **_: Any) -> Optional[Dict[str, Any]]:
    """Return a ``modify`` directive with repaired ``edits``, or ``None`` to leave the call alone.

    Never raises: ``pre_tool_call`` hooks fail CLOSED (an exception blocks the tool), and a broken
    repair must degrade to "no repair", not to a blocked edit."""
    if tool_name != EDIT_NOTE_TOOL or not isinstance(args, dict) or "edits" not in args:
        return None
    try:
        fixed, changed = normalize_edits(args.get("edits"))
    except Exception:
        logger.debug("turbovault edit_note normalize failed", exc_info=True)
        return None
    if not changed:
        return None
    logger.info("turbovault edit_note: normalized malformed SEARCH/REPLACE payload before dispatch")
    return {"action": "modify", "args": {"edits": fixed}}


def register(ctx) -> None:
    ctx.register_hook("pre_tool_call", _on_pre_tool_call)
