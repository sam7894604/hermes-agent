"""Tests for the turbovault-fixups plugin: the edit_note SEARCH/REPLACE normalizer and the
``pre_tool_call`` hook that applies it as a ``modify`` directive.

Regression target: LINE travel-accounting turns where a weak model called
``mcp__turbovault__edit_note`` with malformed ``edits`` payloads
(``SEARCH:``/``REPLACE:`` labels, or ``[{"old_string","new_string"}]`` JSON),
which turbovault rejected with "No SEARCH/REPLACE blocks found in input",
forcing a full-overwrite fallback every edit.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

_PLUGIN_DIR = Path(__file__).resolve().parents[2] / "plugins" / "turbovault-fixups"


def _load_plugin():
    """Import the bundled plugin package the way PluginManager names it, so its relative import works."""
    if "hermes_plugins" not in sys.modules:
        ns = types.ModuleType("hermes_plugins")
        ns.__path__ = []
        sys.modules["hermes_plugins"] = ns
    spec = importlib.util.spec_from_file_location(
        "hermes_plugins.turbovault_fixups", _PLUGIN_DIR / "__init__.py",
        submodule_search_locations=[str(_PLUGIN_DIR)],
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


_plugin = _load_plugin()
normalize_edits = _plugin.normalize_edits

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


# ---------------------------------------------------------------------------
# The hook contract: a ``modify`` directive only for turbovault edit_note with a repairable payload
# ---------------------------------------------------------------------------

def test_hook_rewrites_only_a_repairable_edit_note_payload():
    malformed = "SEARCH:\nold line\nREPLACE:\nnew line\n"
    directive = _plugin._on_pre_tool_call(tool_name=_plugin.EDIT_NOTE_TOOL, args={"path": "a.md", "edits": malformed})
    assert directive and directive["action"] == "modify" and set(directive["args"]) == {"edits"}
    expected, changed = normalize_edits(malformed)
    assert changed and directive["args"]["edits"] == expected
    # The directive is a shallow merge: untouched keys (path) are left to the original args.
    assert "path" not in directive["args"]


def test_hook_leaves_other_tools_and_canonical_payloads_alone():
    canonical, changed = normalize_edits("SEARCH:\nx\nREPLACE:\ny\n")
    assert changed
    assert _plugin._on_pre_tool_call(tool_name=_plugin.EDIT_NOTE_TOOL, args={"edits": canonical}) is None
    assert _plugin._on_pre_tool_call(tool_name="mcp__turbovault__write_note", args={"edits": "SEARCH:\nx\nREPLACE:\ny"}) is None
    assert _plugin._on_pre_tool_call(tool_name="patch", args={"edits": "SEARCH:\nx\nREPLACE:\ny"}) is None
    # pre_tool_call hooks fail closed, so a garbage payload must degrade to "no repair", never raise.
    assert _plugin._on_pre_tool_call(tool_name=_plugin.EDIT_NOTE_TOOL, args={"edits": object()}) is None
    assert _plugin._on_pre_tool_call(tool_name=_plugin.EDIT_NOTE_TOOL, args=None) is None


def test_register_wires_the_pre_tool_call_hook():
    class _Ctx:
        def __init__(self):
            self.hooks = []

        def register_hook(self, name, callback):
            self.hooks.append((name, callback))

    ctx = _Ctx()
    _plugin.register(ctx)
    assert ctx.hooks == [("pre_tool_call", _plugin._on_pre_tool_call)]


# ---------------------------------------------------------------------------
# Bundled-plugin discovery through the real PluginManager (temp HERMES_HOME)
# ---------------------------------------------------------------------------

def _hook_callbacks(mgr, hook_name):
    """Registered callbacks for *hook_name*, whatever record shape the manager keeps."""
    out = []
    for rec in mgr._hooks.get(hook_name, []):
        cb = getattr(rec, "callback", rec)
        out.append(cb)
    return out


def _is_our_hook(cb):
    return getattr(cb, "__name__", "") == "_on_pre_tool_call" and "turbovault_fixups" in getattr(cb, "__module__", "")


def test_bundled_plugin_is_discovered_but_needs_opt_in(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli import plugins as pmod
    mgr = pmod.PluginManager()
    mgr.discover_and_load()
    loaded = mgr._plugins["turbovault-fixups"]
    assert loaded.manifest.source == "bundled"
    assert not loaded.enabled
    assert not any(_is_our_hook(cb) for cb in _hook_callbacks(mgr, "pre_tool_call"))


def test_enabled_bundled_plugin_registers_the_pre_tool_call_hook(tmp_path, monkeypatch):
    import hermes_yaml as yaml

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(yaml.safe_dump({"plugins": {"enabled": ["turbovault-fixups"]}}))
    from hermes_cli import plugins as pmod
    mgr = pmod.PluginManager()
    mgr.discover_and_load()
    loaded = mgr._plugins["turbovault-fixups"]
    assert loaded.enabled, loaded.error
    ours = [cb for cb in _hook_callbacks(mgr, "pre_tool_call") if _is_our_hook(cb)]
    assert len(ours) == 1
    # The registered callback is the live hook: it repairs a malformed edit_note payload.
    directive = ours[0](tool_name="mcp__turbovault__edit_note", args={"edits": "SEARCH:\na\nREPLACE:\nb\n"})
    assert directive and directive["action"] == "modify" and "<<<<<<< SEARCH" in directive["args"]["edits"]
