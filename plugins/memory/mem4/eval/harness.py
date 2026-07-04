"""Repeatable recall-eval harness for mem4 (② measurement, synthetic data).

Loads the fixed QA fixture, indexes each item's ``knowledge`` into a fresh recall
store, runs the ``query`` against it, and scores a hit when a returned snippet
contains ``expect_substr``. Reports recall accuracy (overall / by language /
paraphrase-vs-exact), route distribution (fts/trigram/like), and injected chars.

A/B (design spike §7): the **baseline** arm models "the knowledge has left the
hot zone and there is no recall mechanism" → accuracy ≈ 0 (the A≈0 claim). The
**experiment** arm runs mem4's FTS5 recall. The gate then checks: experiment
recalls cold knowledge the baseline can't, the resident hot zone shrank, and the
net per-query token budget improved — without accuracy regressing.

This runs on SYNTHETIC fixture data. Real hit rates require deploying to
toothless and collecting actual usage (see the deployment draft in the PR
report); the same harness + the Auditor's JSONL then run against real data.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..recall import RecallStore

_FIXTURE_PATH = Path(__file__).with_name("qa_fixture.json")
# A stand-in for the tiny always-resident routing legend (system_prompt_block).
_EXPERIMENT_HOT_CHARS = 260


def load_fixture(path: Optional[Path] = None) -> List[Dict[str, Any]]:
    data = json.loads(Path(path or _FIXTURE_PATH).read_text(encoding="utf-8"))
    return data["items"]


def _index_knowledge(store: RecallStore, items: List[Dict[str, Any]], now: float) -> None:
    for it in items:
        store.index(ref=it["id"], content=it["knowledge"], kind="qa", ts=now)


def run_recall_eval(
    items: List[Dict[str, Any]], *, arm: str = "experiment",
    db_path: str = ":memory:", now: float = 1_780_000_000.0, limit: int = 5,
) -> Dict[str, Any]:
    """Run the fixture through recall for one arm; return a report dict."""
    store = RecallStore(Path(db_path))
    try:
        _index_knowledge(store, items, now)
        rows = []
        for it in items:
            if arm == "baseline":
                hits = []  # cold knowledge, no recall mechanism → A≈0
            else:
                hits = store.search(it["query"], limit=limit, now=now)
            correct = any(it["expect_substr"] in h.snippet for h in hits)
            injected = sum(len(h.snippet) for h in hits)
            rows.append({
                "id": it["id"], "lang": it["lang"], "paraphrase": it["paraphrase"],
                "correct": correct, "route": (hits[0].route if hits else ""),
                "injected_chars": injected,
            })
    finally:
        store.close()
    return _aggregate(rows, arm)


def _aggregate(rows: List[Dict[str, Any]], arm: str) -> Dict[str, Any]:
    def acc(subset):
        return round(sum(1 for r in subset if r["correct"]) / len(subset), 3) if subset else 0.0

    route_dist: Dict[str, int] = {}
    for r in rows:
        if r["route"]:
            route_dist[r["route"]] = route_dist.get(r["route"], 0) + 1

    injected = [r["injected_chars"] for r in rows]
    return {
        "arm": arm,
        "n": len(rows),
        "accuracy": acc(rows),
        "accuracy_en": acc([r for r in rows if r["lang"] == "en"]),
        "accuracy_zh": acc([r for r in rows if r["lang"] == "zh"]),
        "accuracy_exact": acc([r for r in rows if not r["paraphrase"]]),
        "accuracy_paraphrase": acc([r for r in rows if r["paraphrase"]]),
        "route_distribution": route_dist,
        "avg_injected_chars": round(sum(injected) / len(injected), 1) if injected else 0.0,
        "rows": rows,
    }


def evaluate_gate(
    baseline: Dict[str, Any], experiment: Dict[str, Any], *,
    baseline_hot_chars: int, experiment_hot_chars: int = _EXPERIMENT_HOT_CHARS,
    recall_win_threshold: float = 0.3,
) -> Dict[str, Any]:
    """Success/failure line (design spike §7). Returns pass/fail + reasons."""
    reasons = []

    recall_win = experiment["accuracy"] - baseline["accuracy"]
    ok_recall = recall_win >= recall_win_threshold
    reasons.append(
        f"{'PASS' if ok_recall else 'FAIL'} recall of cold knowledge: "
        f"experiment {experiment['accuracy']:.0%} vs baseline {baseline['accuracy']:.0%} "
        f"(Δ={recall_win:+.0%}, need ≥{recall_win_threshold:.0%})"
    )

    ok_resident = experiment_hot_chars < baseline_hot_chars
    reasons.append(
        f"{'PASS' if ok_resident else 'FAIL'} resident hot-zone smaller: "
        f"experiment {experiment_hot_chars} < baseline {baseline_hot_chars} chars"
    )

    # Net per-query cost: baseline pays the full hot zone every query; experiment
    # pays its small legend + on-demand recall injection.
    exp_per_query = experiment_hot_chars + experiment["avg_injected_chars"]
    ok_net_token = exp_per_query < baseline_hot_chars
    reasons.append(
        f"{'PASS' if ok_net_token else 'FAIL'} net per-query chars: "
        f"experiment {exp_per_query:.0f} (legend+recall) < baseline {baseline_hot_chars}"
    )

    passed = ok_recall and ok_resident and ok_net_token
    return {
        "passed": passed,
        "recall_win": round(recall_win, 3),
        "reasons": reasons,
        "verdict": "SHIP (measured value positive)" if passed
                   else "ROLL BACK (remove memory.provider: mem4)",
    }


def run_ab(
    items: Optional[List[Dict[str, Any]]] = None, *,
    db_path: str = ":memory:", baseline_hot_chars: Optional[int] = None,
) -> Dict[str, Any]:
    """Run both arms on the fixture and apply the gate."""
    items = items or load_fixture()
    # Baseline "flat file" hot cost = the whole knowledge base loaded every turn.
    if baseline_hot_chars is None:
        baseline_hot_chars = sum(len(it["knowledge"]) for it in items)
    baseline = run_recall_eval(items, arm="baseline", db_path=db_path)
    experiment = run_recall_eval(items, arm="experiment", db_path=db_path)
    gate = evaluate_gate(baseline, experiment, baseline_hot_chars=baseline_hot_chars)
    return {"baseline": baseline, "experiment": experiment, "gate": gate,
            "baseline_hot_chars": baseline_hot_chars}


def format_report(ab: Dict[str, Any]) -> str:
    exp, base, gate = ab["experiment"], ab["baseline"], ab["gate"]
    lines = [
        "mem4 recall A/B (synthetic fixture)",
        "─" * 44,
        f"  items:              {exp['n']}",
        f"  accuracy  exp/base: {exp['accuracy']:.0%} / {base['accuracy']:.0%}",
        f"    en:               {exp['accuracy_en']:.0%}",
        f"    zh:               {exp['accuracy_zh']:.0%}",
        f"    exact:            {exp['accuracy_exact']:.0%}",
        f"    paraphrase:       {exp['accuracy_paraphrase']:.0%}   (FTS weak spot — Fable 5 §3)",
        f"  route distribution: {exp['route_distribution']}",
        f"  avg injected chars: {exp['avg_injected_chars']:.0f}",
        f"  baseline hot chars: {ab['baseline_hot_chars']}",
        "",
        f"  GATE: {gate['verdict']}",
    ]
    lines += [f"    - {r}" for r in gate["reasons"]]
    lines.append("")
    lines.append("  NOTE: synthetic data. Real hit rates need toothless deployment"
                 " + actual usage (see deployment draft).")
    return "\n".join(lines)


def main() -> None:  # pragma: no cover - manual invocation
    print(format_report(run_ab()))


if __name__ == "__main__":  # pragma: no cover
    main()
