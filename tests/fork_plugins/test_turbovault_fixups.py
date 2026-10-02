"""Tests for the turbovault-fixups plugin: the edit_note SEARCH/REPLACE normalizer, the
``pre_tool_call`` hook that applies it as a ``modify`` directive, and the vault-write rule that skips
a doomed local patch (what used to be a core change in agent/turn_*.py).

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

_PLUGIN_DIR = Path(__file__).resolve().parents[2] / "fork-plugins" / "turbovault-fixups"


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


def test_register_wires_both_hooks():
    class _Ctx:
        def __init__(self):
            self.hooks = []

        def register_hook(self, name, callback):
            self.hooks.append((name, callback))

    ctx = _Ctx()
    _plugin.register(ctx)
    assert ctx.hooks == [("pre_tool_call", _plugin._on_pre_tool_call), ("post_tool_call", _plugin._on_post_tool_call)]


# ---------------------------------------------------------------------------
# Vault-write rule: the doomed local patch is skipped, the verifier never sees a failure
# ---------------------------------------------------------------------------

import json
import pytest


@pytest.fixture(autouse=True)
def _fresh_vault_state():
    _plugin._vault_write_at.clear()
    yield
    _plugin._vault_write_at.clear()


def _vault_ok(session_id, tool=_plugin.WRITE_NOTE_TOOL):
    _plugin._on_post_tool_call(tool_name=tool, args={"path": "生活/旅遊/x.md"},
                               result=json.dumps({"success": True}), session_id=session_id)


def test_patch_on_missing_path_is_blocked_only_after_a_vault_write_in_that_session(tmp_path):
    missing = str(tmp_path / "生活" / "旅遊" / "x.md")
    patch_args = {"mode": "replace", "path": missing, "old_string": "a", "new_string": "b"}
    # No vault write yet: core keeps its normal behaviour (the patch fails and the verifier reports it).
    assert _plugin._on_pre_tool_call(tool_name="patch", args=patch_args, session_id="s1") is None
    _vault_ok("s1")
    directive = _plugin._on_pre_tool_call(tool_name="patch", args=patch_args, session_id="s1")
    assert directive and directive["action"] == "block" and missing in directive["message"]
    # Another session is untouched.
    assert _plugin._on_pre_tool_call(tool_name="patch", args=patch_args, session_id="s2") is None


def test_existing_local_files_and_other_tools_are_never_blocked(tmp_path):
    real = tmp_path / "notes.md"
    real.write_text("x", encoding="utf-8")
    _vault_ok("s1", tool=_plugin.EDIT_NOTE_TOOL)
    assert _plugin._on_pre_tool_call(tool_name="patch", args={"path": str(real), "old_string": "x", "new_string": "y"}, session_id="s1") is None
    assert _plugin._on_pre_tool_call(tool_name="write_file", args={"path": str(tmp_path / "new.md"), "content": "y"}, session_id="s1") is None
    assert _plugin._on_pre_tool_call(tool_name="terminal", args={"command": "ls"}, session_id="s1") is None


def test_failed_vault_writes_do_not_arm_the_rule(tmp_path):
    missing = str(tmp_path / "nope.md")
    _plugin._on_post_tool_call(tool_name=_plugin.EDIT_NOTE_TOOL, args={},
                               result=json.dumps({"error": "Parse error: No SEARCH/REPLACE blocks found in input"}),
                               session_id="s1")
    _plugin._on_post_tool_call(tool_name=_plugin.WRITE_NOTE_TOOL, args={}, result="ok", session_id="s1", is_error=True)
    assert _plugin._on_pre_tool_call(tool_name="patch", args={"path": missing}, session_id="s1") is None


def test_protection_expires(tmp_path, monkeypatch):
    missing = str(tmp_path / "nope.md")
    _vault_ok("s1")
    monkeypatch.setattr(_plugin, "VAULT_WRITE_TTL_S", 0)
    monkeypatch.setattr(_plugin.time, "monotonic", lambda: _plugin._vault_write_at["s1"] + 1)
    assert _plugin._on_pre_tool_call(tool_name="patch", args={"path": missing}, session_id="s1") is None


# ---------------------------------------------------------------------------
# Discovery as a USER plugin (how fork-plugins/install.sh deploys it)
# ---------------------------------------------------------------------------

def _hook_callbacks(mgr, hook_name):
    return [getattr(rec, "callback", rec) for rec in mgr._hooks.get(hook_name, [])]


def _is_our_hook(cb, name):
    return getattr(cb, "__name__", "") == name and "turbovault_fixups" in getattr(cb, "__module__", "")


def test_user_dir_copy_is_discovered_and_registers_both_hooks_when_enabled(tmp_path, monkeypatch):
    import shutil
    import hermes_yaml as yaml

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    shutil.copytree(_PLUGIN_DIR, tmp_path / "plugins" / "turbovault-fixups")
    from hermes_cli import plugins as pmod
    mgr = pmod.PluginManager()
    mgr.discover_and_load()
    loaded = mgr._plugins["turbovault-fixups"]
    assert loaded.manifest.source == "user" and not loaded.enabled  # opt-in, like every user plugin

    (tmp_path / "config.yaml").write_text(yaml.safe_dump({"plugins": {"enabled": ["turbovault-fixups"]}}))
    mgr = pmod.PluginManager()
    mgr.discover_and_load()
    loaded = mgr._plugins["turbovault-fixups"]
    assert loaded.enabled, loaded.error
    assert sum(_is_our_hook(cb, "_on_pre_tool_call") for cb in _hook_callbacks(mgr, "pre_tool_call")) == 1
    assert sum(_is_our_hook(cb, "_on_post_tool_call") for cb in _hook_callbacks(mgr, "post_tool_call")) == 1
