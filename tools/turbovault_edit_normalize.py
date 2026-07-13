"""Normalize malformed turbovault ``edit_note`` payloads into canonical
aider SEARCH/REPLACE blocks.

The turbovault MCP ``edit_note`` tool requires its ``edits`` string to contain
aider-style conflict-marker blocks::

    <<<<<<< SEARCH
    <old text>
    =======
    <new text>
    >>>>>>> REPLACE

Weak models frequently emit other shapes instead — ``SEARCH:`` / ``REPLACE:``
label lines, or a ``[{"old_string": ..., "new_string": ...}]`` JSON array
borrowed from the local file-edit tool — which turbovault rejects with
``{"error": "Parse error: No SEARCH/REPLACE blocks found in input"}``. The
agent then falls back to a full-note ``write_note`` overwrite every time.

This module rewrites the known malformed shapes into canonical blocks *before*
the MCP call is dispatched, so a targeted edit actually lands.

Design contract — purely additive and non-destructive:
    * If the input already contains canonical markers → returned UNCHANGED.
    * If it matches no known malformed shape → returned UNCHANGED.
The normalizer never corrupts a payload it doesn't understand, so turbovault's
own parser stays the final authority and the existing fallback-to-write_note
path (and the local-file vault fallback) are preserved.
"""

from __future__ import annotations

import json
import re
from typing import List, Optional, Tuple

_SEARCH_MARKER = "<<<<<<< SEARCH"
_DIVIDER = "======="
_REPLACE_MARKER = ">>>>>>> REPLACE"


def _build_block(search: str, replace: str) -> str:
    return f"{_SEARCH_MARKER}\n{search}\n{_DIVIDER}\n{replace}\n{_REPLACE_MARKER}"


def _already_canonical(text: str) -> bool:
    return _SEARCH_MARKER in text and _REPLACE_MARKER in text


def _pairs_to_blocks(pairs: List[Tuple[str, str]]) -> Optional[str]:
    # A usable block needs a non-empty SEARCH side (turbovault matches on it);
    # an empty REPLACE side is legal (pure deletion).
    blocks = [_build_block(s, r) for s, r in pairs if s.strip()]
    if not blocks:
        return None
    return "\n\n".join(blocks)


def _from_json(text: str) -> Optional[str]:
    """``[{"old_string": .., "new_string": ..}]`` (local file-tool schema)."""
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return None
    items = data if isinstance(data, list) else [data]
    pairs: List[Tuple[str, str]] = []
    for it in items:
        if not isinstance(it, dict):
            return None
        s = it.get("old_string")
        if s is None:
            s = it.get("search")
        if s is None:
            s = it.get("old")
        r = it.get("new_string")
        if r is None:
            r = it.get("replace")
        if r is None:
            r = it.get("new")
        if s is None and r is None:
            return None
        pairs.append((str(s or ""), str(r or "")))
    return _pairs_to_blocks(pairs)


# ``SEARCH: <old> REPLACE: <new>`` label form. Non-greedy search side up to the
# first REPLACE:, replace side up to the next SEARCH: (supports >1 pair). The
# search/replace bodies may span multiple lines (DOTALL).
_LABEL_RE = re.compile(
    r"SEARCH[ \t]*:(?P<search>.*?)REPLACE[ \t]*:(?P<replace>.*?)(?=SEARCH[ \t]*:|\Z)",
    re.DOTALL | re.IGNORECASE,
)


def _strip_edge(text: str) -> str:
    text = text.strip("\n")
    if text.startswith(" "):
        text = text[1:]
    return text


def _from_labels(text: str) -> Optional[str]:
    pairs: List[Tuple[str, str]] = []
    for m in _LABEL_RE.finditer(text):
        pairs.append((_strip_edge(m.group("search")), _strip_edge(m.group("replace"))))
    if not pairs:
        return None
    return _pairs_to_blocks(pairs)


def normalize_edits(edits: object) -> Tuple[object, bool]:
    """Return ``(normalized_edits, changed)``.

    Non-string input, empty input, already-canonical text, or an unrecognized
    shape is returned unchanged with ``changed=False`` — never raises.
    """
    if not isinstance(edits, str) or not edits.strip():
        return edits, False
    if _already_canonical(edits):
        return edits, False
    stripped = edits.lstrip()
    if stripped.startswith("[") or stripped.startswith("{"):
        out = _from_json(edits)
        if out:
            return out, True
    out = _from_labels(edits)
    if out:
        return out, True
    return edits, False
