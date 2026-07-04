"""Tests for mem4 ② — Auditor instrumentation, A/B arm, QA harness, Baserow sink."""

import json

from plugins.memory.mem4 import Mem4MemoryProvider
from plugins.memory.mem4.audit import Auditor, estimate_tokens
from plugins.memory.mem4.eval.harness import run_ab, load_fixture, evaluate_gate


def _audit_provider(tmp_path, arm="experiment"):
    return Mem4MemoryProvider({
        "backend": "local-file",
        "dream": {"enabled": False},
        "audit": {"enabled": True},
        "arm": arm,
    })


# -- instrumentation ---------------------------------------------------------

def test_search_event_recorded(tmp_path):
    p = _audit_provider(tmp_path)
    p.initialize("s1", hermes_home=str(tmp_path))
    p.sync_turn("the deploy target host is toothless on lightnode", "ok")
    p.handle_tool_call("mem_search", {"query": "toothless lightnode"})
    p.handle_tool_call("mem_search", {"query": "zzz nonexistent term qqq"})

    events = p._auditor.read_events()
    searches = [e for e in events if e["kind"] == "search"]
    assert len(searches) == 2
    hit, miss = searches[0], searches[1]
    assert hit["hit"] is True and hit["route"] in {"fts", "trigram", "like"}
    assert hit["injected_chars"] > 0 and hit["hit_estimated"] is False
    assert miss["hit"] is False and miss["route"] == ""
    p.shutdown()


def test_route_and_prefetch_events_recorded(tmp_path):
    root = tmp_path / "mem4"
    root.mkdir()
    (root / "sys.md").write_text("host toothless lightnode vps tokyo", encoding="utf-8")
    p = _audit_provider(tmp_path)
    p.initialize("s1", hermes_home=str(tmp_path))

    p.handle_tool_call("mem_route", {"code": "sys"})       # hit
    p.handle_tool_call("mem_route", {"code": "nope"})      # miss
    p.prefetch("toothless lightnode vps tokyo host")       # prefetch hit

    events = p._auditor.read_events()
    routes = [e for e in events if e["kind"] == "route"]
    prefetches = [e for e in events if e["kind"] == "prefetch"]
    assert [r["hit"] for r in routes] == [True, False]
    assert routes[0]["route"] == "microfile"
    assert prefetches and prefetches[0]["prefetch_triggered"] is True
    # injected_tokens is present and derived from chars
    assert prefetches[0]["injected_tokens"] == estimate_tokens(prefetches[0]["injected_chars"])
    p.shutdown()


def test_audit_disabled_writes_nothing(tmp_path):
    p = Mem4MemoryProvider({"backend": "local-file", "dream": {"enabled": False}})
    p.initialize("s1", hermes_home=str(tmp_path))  # audit default off
    p.sync_turn("remember the deploy host is toothless", "ok")
    p.handle_tool_call("mem_search", {"query": "toothless"})
    assert not (tmp_path / "mem4" / "audit.jsonl").exists()
    p.shutdown()


# -- A/B arm -----------------------------------------------------------------

def test_baseline_arm_disables_agent_surfaces(tmp_path):
    p = _audit_provider(tmp_path, arm="baseline")
    p.initialize("s1", hermes_home=str(tmp_path))
    assert p._is_baseline() is True
    # No tools, no system-prompt injection, no prefetch injection.
    assert p.get_tool_schemas() == []
    assert p.system_prompt_block() == ""
    p.sync_turn("deploy host toothless lightnode server", "ok")
    assert p.prefetch("toothless lightnode server host deploy") == ""
    p.shutdown()


def test_experiment_arm_enables_surfaces(tmp_path):
    p = _audit_provider(tmp_path, arm="experiment")
    p.initialize("s1", hermes_home=str(tmp_path))
    assert [s["name"] for s in p.get_tool_schemas()] == ["mem_route", "mem_search"]
    assert "mem_search" in p.system_prompt_block()
    p.shutdown()


# -- summarize + Baserow sink (mock) -----------------------------------------

def test_summarize_and_baserow_row_uses_existing_columns(tmp_path):
    auditor = Auditor(tmp_path / "audit.jsonl", enabled=True, arm="experiment", session_id="s1")
    auditor.record_search("q1", route="fts", hit=True, injected_chars=100)
    auditor.record_search("q2", route="like", hit=True, injected_chars=60)
    auditor.record_search("q3", route="", hit=False, injected_chars=0)

    summary = auditor.summarize(auditor.read_events())
    assert summary["n_search"] == 3
    assert summary["search_hit_rate"] == round(2 / 3, 3)
    assert summary["route_distribution"] == {"fts": 1, "like": 1, "none": 1}

    captured = {}

    def mock_writer(table_id, rows):
        captured["table_id"] = table_id
        captured["rows"] = rows

    row = auditor.export_to_baserow(mock_writer, date_str="2026-07-05", name="mem4 audit test")
    assert captured["table_id"] == 907
    # Only columns that exist on table 907.
    existing_907 = {
        "Name", "Notes", "Active", "date", "type", "mem_chars", "entry_count",
        "mem_pct", "hot_hit_rate", "est_tokens_saved", "dead_links",
        "items_archived", "items_removed", "notes",
    }
    assert set(row).issubset(existing_907)
    assert row["type"] == "audit"
    assert json.loads(row["notes"])["arm"] == "experiment"


# -- QA harness --------------------------------------------------------------

def test_fixture_has_enough_items_incl_paraphrase_and_cjk():
    items = load_fixture()
    assert len(items) >= 20
    assert any(it["paraphrase"] for it in items)
    assert any(it["lang"] == "zh" for it in items)
    assert any(it["lang"] == "en" for it in items)


def test_harness_ab_experiment_beats_baseline():
    ab = run_ab()
    assert ab["baseline"]["accuracy"] == 0.0          # cold knowledge, no recall
    assert ab["experiment"]["accuracy"] > 0.5         # FTS5 recalls most exact-term
    # All three routes exercised by the fixture.
    dist = ab["experiment"]["route_distribution"]
    assert set(dist).issuperset({"fts", "trigram", "like"})
    assert ab["gate"]["passed"] is True
    assert ab["gate"]["recall_win"] > 0.3


def test_gate_rolls_back_when_no_recall_advantage():
    # Construct reports where experiment has no recall edge → gate must fail.
    base = {"accuracy": 0.0, "avg_injected_chars": 0.0}
    exp = {"accuracy": 0.05, "avg_injected_chars": 50.0}
    gate = evaluate_gate(base, exp, baseline_hot_chars=1000, experiment_hot_chars=260)
    assert gate["passed"] is False
    assert "ROLL BACK" in gate["verdict"]
