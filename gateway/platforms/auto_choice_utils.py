"""Shared utilities for auto-detected inline choice buttons.

Both the Discord and Telegram adapters use the same ``? ``/``?? `` prefix
convention to gate clickable choice buttons on agent replies that already
contain a question + option list.  Extracting the detection logic here keeps
the two adapters in sync without copy-paste drift.

Public surface
--------------
``_detect_inline_choices(content, *, max_choices)``
    Primary entry point.  Returns ``(choices, multi_select)``.

All other names are implementation details used internally.
"""

from __future__ import annotations

import re
from typing import List, Optional

# ---------------------------------------------------------------------------
# Prefix constants
# ---------------------------------------------------------------------------

# Explicit format prefixes the agent uses to opt a reply into clickable
# buttons. A line starting with ``? `` (one question mark + space) marks a
# single-choice prompt; ``?? `` (two question marks + space) marks a
# multi-select prompt. Without one of these prefixes the reply is left alone,
# so incidental numbered/bulleted lists (deploy steps, citations, guesses)
# never sprout buttons.
_SINGLE_CHOICE_PREFIX = "? "
_MULTI_CHOICE_PREFIX = "?? "

# ---------------------------------------------------------------------------
# Line-pattern constants
# ---------------------------------------------------------------------------

# Circled-number markers (①..⑳) used by some models for option lists.
_CIRCLED_NUMBER_MARKERS = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳"

# Ordered by strength of the "this is a menu" signal. The first style that
# yields >=2 options wins, so an explicit numbered menu is preferred over a
# loose bullet list that happens to share the message.
_CHOICE_LINE_PATTERNS = (
    re.compile(r"^\s*\d{1,2}\s*[.)、．]\s+(?P<text>\S.*)$"),
    re.compile(r"^\s*[" + _CIRCLED_NUMBER_MARKERS + r"]\s*(?P<text>\S.*)$"),
    re.compile(r"^\s*[A-Za-z]\s*[).]\s+(?P<text>\S.*)$"),
    re.compile(r"^\s*[-*•·]\s+(?P<text>\S.*)$"),
)

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _dedupe_keep_order(items: List[str]) -> List[str]:
    seen: set = set()
    out: List[str] = []
    for it in items:
        if it not in seen:
            seen.add(it)
            out.append(it)
    return out


def _clean_choice_text(text: str) -> str:
    """Trim a parsed option down to a clean button label body.

    Strips surrounding markdown emphasis (``**bold**``, ``*italic*``,
    ``` `code` ```) that would otherwise bloat the label, plus trailing
    list punctuation.
    """
    opt = text.strip()
    opt = opt.strip("*_`").strip()
    opt = opt.rstrip(" ,，、;；")
    return opt.strip()


def _detect_choice_prefix(text: str) -> Optional[bool]:
    """Return the choice mode declared by a leading prefix line, or ``None``.

    Scans lines for the first ``?? ``/``? `` prefix. Returns ``True`` for
    multi-select, ``False`` for single-select, ``None`` when neither prefix is
    present (i.e. the reply is not opting into buttons).
    """
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith(_MULTI_CHOICE_PREFIX):
            return True
        if stripped.startswith(_SINGLE_CHOICE_PREFIX):
            return False
    return None


def _extract_choice_question(content: str) -> str:
    """Pull the question text off the prefix line, sans ``? ``/``?? `` marker.

    ``"? 你想要什麼服務？"`` → ``"你想要什麼服務？"``. Returns ``""`` if no
    prefix line is found.
    """
    if not content:
        return ""
    for line in content.strip().splitlines():
        stripped = line.strip()
        if stripped.startswith(_MULTI_CHOICE_PREFIX):
            return stripped[len(_MULTI_CHOICE_PREFIX):].strip()
        if stripped.startswith(_SINGLE_CHOICE_PREFIX):
            return stripped[len(_SINGLE_CHOICE_PREFIX):].strip()
    return ""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _detect_inline_choices(
    content: str, *, max_choices: int = 24,
) -> "tuple[List[str], bool]":
    """Extract clickable choices from an outbound message.

    Detection is gated on an explicit prefix line — ``? `` for single-select
    or ``?? `` for multi-select. Without a prefix nothing is returned, so
    incidental numbered lists (step-by-step instructions, citations, guesses)
    don't sprout buttons.

    Returns ``(choices, multi_select)``: clean option bodies WITHOUT their
    leading markers (the view adds its own ``1.``/``2.`` numbering), capped at
    ``max_choices``, and a flag indicating multi-select mode. ``([], False)``
    when no prefix is present or fewer than two options parse out.
    """
    if not content or not content.strip():
        return [], False
    text = content.strip()

    mode = _detect_choice_prefix(text)
    if mode is None:
        return [], False
    multi_select = mode

    lines = text.splitlines()
    for pattern in _CHOICE_LINE_PATTERNS:
        items: List[str] = []
        for line in lines:
            m = pattern.match(line)
            if m:
                opt = _clean_choice_text(m.group("text"))
                if opt:
                    items.append(opt)
        items = _dedupe_keep_order(items)
        if len(items) >= 2:
            return items[:max_choices], multi_select

    return [], multi_select
