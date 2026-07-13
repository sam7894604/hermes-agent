"""Tests for the turbovault edit_note SEARCH/REPLACE normalizer.

Regression target: LINE travel-accounting turns where a weak model called
``mcp__turbovault__edit_note`` with malformed ``edits`` payloads
(``SEARCH:``/``REPLACE:`` labels, or ``[{"old_string","new_string"}]`` JSON),
which turbovault rejected with "No SEARCH/REPLACE blocks found in input",
forcing a full-overwrite fallback every edit.
"""

from __future__ import annotations

from tools.turbovault_edit_normalize import normalize_edits

_S = "<<<<<<< SEARCH"
_D = "======="
_R = ">>>>>>> REPLACE"


def _blocks(text):
    """Parse canonical output into a list of (search, replace) tuples."""
    out = []
    for chunk in text.split(_S):
        chunk = chunk.strip("\n")
        if not chunk or _D not in chunk:
            continue
        search, rest = chunk.split("\n" + _D + "\n", 1)
        replace = rest.rsplit("\n" + _R, 1)[0]
        out.append((search, replace))
    return out


class TestPassthrough:
    def test_already_canonical_unchanged(self):
        canon = f"{_S}\nold\n{_D}\nnew\n{_R}"
        out, changed = normalize_edits(canon)
        assert changed is False and out == canon

    def test_unrecognized_unchanged(self):
        out, changed = normalize_edits("just some prose, no edits here")
        assert changed is False and out == "just some prose, no edits here"

    def test_non_string_unchanged(self):
        out, changed = normalize_edits(None)
        assert changed is False and out is None

    def test_empty_unchanged(self):
        out, changed = normalize_edits("   ")
        assert changed is False


class TestLabelForm:
    def test_single_line_colon_labels(self):
        # The exact shape seen in production (7/12 18:32).
        raw = "SEARCH: | 超市 | 240,975 | 叔鼠先付 |\nREPLACE: | 超市 | 240,975 | 潔宜先付 |"
        out, changed = normalize_edits(raw)
        assert changed is True
        pairs = _blocks(out)
        assert pairs == [("| 超市 | 240,975 | 叔鼠先付 |", "| 超市 | 240,975 | 潔宜先付 |")]

    def test_multiline_search_and_replace(self):
        # Search/replace bodies span multiple lines (7/12 18:28 shape).
        raw = (
            "SEARCH: | 押金 | 1,000,000 |\n\n## Day 7 — 7/13\n"
            "REPLACE: | 押金 | 1,000,000 |\n| 超市 | 240,975 |\n\n## Day 7 — 7/13"
        )
        out, changed = normalize_edits(raw)
        assert changed is True
        (search, replace), = _blocks(out)
        assert search == "| 押金 | 1,000,000 |\n\n## Day 7 — 7/13"
        assert "| 超市 | 240,975 |" in replace

    def test_multiple_pairs(self):
        raw = "SEARCH: a\nREPLACE: A\nSEARCH: b\nREPLACE: B"
        out, changed = normalize_edits(raw)
        assert changed is True
        assert _blocks(out) == [("a", "A"), ("b", "B")]

    def test_case_insensitive_labels(self):
        raw = "Search: x\nReplace: y"
        out, changed = normalize_edits(raw)
        assert changed is True and _blocks(out) == [("x", "y")]


class TestJsonForm:
    def test_old_new_string_array(self):
        # The local file-tool schema mistakenly handed to edit_note (7/12 18:28:38).
        raw = '[{"old_string": "| 押金 | 1,000,000 |", "new_string": "| 押金 | 1,000,000 |\\n| 超市 |"}]'
        out, changed = normalize_edits(raw)
        assert changed is True
        (search, replace), = _blocks(out)
        assert search == "| 押金 | 1,000,000 |"
        assert replace == "| 押金 | 1,000,000 |\n| 超市 |"

    def test_single_object_not_array(self):
        raw = '{"old_string": "foo", "new_string": "bar"}'
        out, changed = normalize_edits(raw)
        assert changed is True and _blocks(out) == [("foo", "bar")]

    def test_search_replace_keys(self):
        raw = '[{"search": "foo", "replace": "bar"}]'
        out, changed = normalize_edits(raw)
        assert changed is True and _blocks(out) == [("foo", "bar")]

    def test_multiple_json_edits(self):
        raw = '[{"old_string": "a", "new_string": "A"}, {"old_string": "b", "new_string": "B"}]'
        out, changed = normalize_edits(raw)
        assert changed is True and _blocks(out) == [("a", "A"), ("b", "B")]

    def test_empty_search_side_dropped(self):
        # A block with a blank SEARCH can never match — drop it, report no change.
        raw = '[{"old_string": "", "new_string": "bar"}]'
        out, changed = normalize_edits(raw)
        assert changed is False and out == raw
