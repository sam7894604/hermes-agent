"""turbovault-fixups — keep turbovault (Obsidian vault over MCP) edits from tripping core safety nets.

Two ``pre_tool_call`` rules plus one ``post_tool_call`` observer, all fail-open:

1. **Repair malformed ``edit_note`` payloads.** Weak models often emit ``SEARCH:``/``REPLACE:``
   labels or ``old_string``/``new_string`` JSON instead of aider SEARCH/REPLACE blocks, which the
   tool rejects ("No SEARCH/REPLACE blocks found in input") and the agent then falls back to a
   full-note overwrite. The hook rewrites the known malformed shapes before dispatch
   (``{"action": "modify"}``); anything unrecognized passes through untouched so turbovault stays
   the final authority on what is valid.
2. **Skip the spurious local ``patch`` after a vault write landed.** After ``write_note`` /
   ``edit_note`` succeeds the model sometimes also issues a local ``patch`` on the raw vault path
   (``/root/生活/旅遊/x.md``), which cannot exist locally. That patch fails, and core's
   file-mutation verifier then appends a "files were NOT modified" footer to a reply whose content
   DID land — in the vault. Blocking the doomed patch (only when the path does not exist locally and
   a vault write succeeded in this session recently) keeps the verifier honest without touching
   core: nothing failed, so there is nothing to warn about.

MCP tools reach the same hooks as built-ins under their registry name (``mcp__<server>__<tool>``);
``{"action": "modify"}`` and ``{"action": "block"}`` are the documented directives
(website/docs/user-guide/features/hooks.md).
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Dict, Optional

from .normalize import normalize_edits

logger = logging.getLogger(__name__)

EDIT_NOTE_TOOL = "mcp__turbovault__edit_note"
WRITE_NOTE_TOOL = "mcp__turbovault__write_note"
VAULT_WRITE_TOOLS = frozenset({EDIT_NOTE_TOOL, WRITE_NOTE_TOOL})
# Local tools whose failure on a vault path the verifier would report. ``write_file`` is left alone:
# creating a local file after a vault write can be a legitimate request.
_LOCAL_PATCH_TOOLS = frozenset({"patch"})
# How long a successful vault write keeps protecting the session from doomed local patches.
VAULT_WRITE_TTL_S = 15 * 60

_lock = threading.Lock()
_vault_write_at: Dict[str, float] = {}  # session_id -> monotonic time of the last successful vault write


def _result_failed(result: Any) -> bool:
    """turbovault reports failures as ``{"error": ...}`` / ``"success": false`` JSON (or an is_error flag)."""
    text = result if isinstance(result, str) else str(result or "")
    head = text.lstrip()[:400].lower()
    return head.startswith('{"error"') or '"success": false' in head or '"success":false' in head


def _on_post_tool_call(tool_name: str = "", args: Optional[Dict[str, Any]] = None, result: Any = None,
                       session_id: str = "", is_error: bool = False, **_: Any) -> None:
    if tool_name not in VAULT_WRITE_TOOLS or not session_id or is_error or _result_failed(result):
        return
    with _lock:
        _vault_write_at[session_id] = time.monotonic()


def _recent_vault_write(session_id: str) -> bool:
    if not session_id:
        return False
    with _lock:
        stamp = _vault_write_at.get(session_id)
    return stamp is not None and (time.monotonic() - stamp) <= VAULT_WRITE_TTL_S


def _on_pre_tool_call(tool_name: str = "", args: Optional[Dict[str, Any]] = None, session_id: str = "",
                      **_: Any) -> Optional[Dict[str, Any]]:
    """``modify`` for a repairable edit_note payload, ``block`` for a doomed local patch after a vault
    write, else ``None``. Never raises: ``pre_tool_call`` hooks fail CLOSED (an exception blocks the
    tool), and a broken rule must degrade to "no opinion", not to a blocked edit."""
    try:
        if tool_name == EDIT_NOTE_TOOL and isinstance(args, dict) and "edits" in args:
            fixed, changed = normalize_edits(args.get("edits"))
            if changed:
                logger.info("turbovault edit_note: normalized malformed SEARCH/REPLACE payload before dispatch")
                return {"action": "modify", "args": {"edits": fixed}}
            return None
        if tool_name in _LOCAL_PATCH_TOOLS and isinstance(args, dict) and _recent_vault_write(session_id):
            path = str(args.get("path") or "")
            if path and not os.path.exists(path):
                logger.info("turbovault-fixups: skipping local patch on missing path %s after a vault write", path)
                return {"action": "block", "message": (
                    f"Skipped: {path} does not exist locally. The note content was already written to the "
                    "vault via turbovault (write_note/edit_note) this session; use edit_note for further "
                    "changes instead of a local patch.")}
    except Exception:
        logger.debug("turbovault-fixups hook failed", exc_info=True)
    return None


def register(ctx) -> None:
    ctx.register_hook("pre_tool_call", _on_pre_tool_call)
    ctx.register_hook("post_tool_call", _on_post_tool_call)
