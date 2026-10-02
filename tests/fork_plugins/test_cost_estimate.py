"""cost-estimate dashboard plugin (fork-plugins/cost-estimate): per-tier pricing + projection math,
the session aggregate query, and the /estimate route."""
from __future__ import annotations

import importlib.util
import sys
import tempfile
import time
import types
from decimal import Decimal
from pathlib import Path

import pytest

from hermes_state import SessionDB

_DASH = Path(__file__).resolve().parents[2] / "fork-plugins" / "cost-estimate" / "dashboard"


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, _DASH / filename)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


_math = _load("hermes_fork_cost_math_under_test", "cost_math.py")
_api = _load("hermes_dashboard_plugin_cost_estimate_under_test", "plugin_api.py")
compute_cost_estimate = _math.compute_cost_estimate


class _PE:
    input_cost_per_million = Decimal("5.00")
    output_cost_per_million = Decimal("25.00")
    cache_read_cost_per_million = Decimal("0.50")
    cache_write_cost_per_million = Decimal("6.25")


def _groups():
    return [
        {"model": "claude", "billing_provider": "anthropic", "billing_base_url": None,
         "sessions": 2, "input_tokens": 1_000_000, "output_tokens": 200_000,
         "cache_read_tokens": 4_000_000, "cache_write_tokens": 0, "reasoning_tokens": 50_000,
         "estimated_cost_usd": 9.9},
        {"model": "mystery", "billing_provider": "x", "billing_base_url": None,
         "sessions": 1, "input_tokens": 100, "output_tokens": 50, "cache_read_tokens": 0,
         "cache_write_tokens": 0, "reasoning_tokens": 0, "estimated_cost_usd": 0.42},
    ]


def test_cost_breakdown_and_projection():
    est = compute_cost_estimate(_groups(), 7 * 86400, lambda m, p, b: _PE() if m == "claude" else None)
    assert est["models"][0]["cost_usd"] == 12.0  # 5 + 5 + 2
    assert est["models"][0]["cost_source"] == "pricing"
    # unpriced model falls back to stored estimate.
    assert est["models"][1]["cost_usd"] == 0.42
    assert est["models"][1]["cost_source"] == "stored_estimate"
    assert est["has_unpriced_models"] is True
    assert est["total_cost_usd"] == 12.42
    assert est["cost_by_tier"] == {"input": 5.0, "output": 5.0, "cache": 2.0}
    assert est["projection"]["daily_usd"] == round(12.42 / 7, 6)
    assert est["projection"]["monthly_usd"] == round(round(12.42 / 7, 6) * 30, 6)


def test_empty_groups():
    est = compute_cost_estimate([], 86400, lambda m, p, b: None)
    assert est["total_cost_usd"] == 0 and est["models"] == []


def test_lookup_exception_falls_back():
    def boom(m, p, b):
        raise RuntimeError("pricing service down")
    est = compute_cost_estimate(_groups()[:1], 86400, boom)
    assert est["models"][0]["cost_source"] == "stored_estimate"
    assert est["models"][0]["cost_usd"] == 9.9


@pytest.fixture()
def db():
    d = SessionDB(db_path=Path(tempfile.mkdtemp()) / "cost.db")
    yield d
    d.close()


def test_aggregates_group_sessions_by_model_within_the_window(db):
    db.create_session(session_id="s1", source="cli", model="m1")
    db.update_token_counts("s1", input_tokens=1000, output_tokens=200,
                           cache_read_tokens=500, estimated_cost_usd=0.01, model="m1")
    db.create_session(session_id="s2", source="cli", model="m1")
    db.update_token_counts("s2", input_tokens=10, output_tokens=5, estimated_cost_usd=0.001, model="m1")
    groups = _api.session_cost_aggregates(db, time.time() - 3600)
    by_model = {g["model"]: g for g in groups}
    assert by_model["m1"]["sessions"] == 2
    assert by_model["m1"]["input_tokens"] == 1010 and by_model["m1"]["output_tokens"] == 205
    assert by_model["m1"]["cache_read_tokens"] == 500
    assert by_model["m1"]["estimated_cost_usd"] == pytest.approx(0.011)
    # Sessions outside the window are not counted.
    assert _api.session_cost_aggregates(db, time.time() + 60) == []


def test_estimate_for_db_has_the_response_contract(db):
    db.create_session(session_id="s1", source="cli", model="m1")
    db.update_token_counts("s1", input_tokens=1000, output_tokens=200, estimated_cost_usd=0.01, model="m1")
    body = _api.estimate_for_db(db, "24h")
    assert body["window"] == "24h"
    assert {"generated_at", "total_cost_usd", "cost_by_tier", "projection", "has_unpriced_models", "models"} <= set(body)
    assert body["models"][0]["model"] == "m1" and body["models"][0]["tokens"]["input"] == 1000


def test_estimate_route_serves_json_and_rejects_bad_windows(db, monkeypatch):
    from contextlib import contextmanager

    from fastapi import FastAPI
    from starlette.testclient import TestClient

    @contextmanager
    def _scope(profile):
        yield

    monkeypatch.setattr(_api, "_profile_scope", _scope)
    monkeypatch.setattr(_api, "_open_session_db_for_profile", lambda profile, read_only=True: db)
    monkeypatch.setattr(_api, "_session_db_path_for_profile", lambda profile: db.db_path)
    # ``db.close()`` runs per request; keep the fixture's connection usable by neutralizing it.
    monkeypatch.setattr(db, "close", lambda: None)
    db.create_session(session_id="s1", source="cli", model="m1")
    db.update_token_counts("s1", input_tokens=1000, output_tokens=200, estimated_cost_usd=0.01, model="m1")

    app = FastAPI()
    app.include_router(_api.router, prefix="/api/plugins/cost-estimate")
    client = TestClient(app)
    ok = client.get("/api/plugins/cost-estimate/estimate?window=7d")
    assert ok.status_code == 200 and ok.json()["window"] == "7d" and ok.json()["models"][0]["model"] == "m1"
    assert client.get("/api/plugins/cost-estimate/estimate?window=invalid").status_code == 422


def test_dashboard_discovers_the_user_dir_copy_with_its_api(tmp_path, monkeypatch):
    import shutil

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    shutil.copytree(_DASH.parent, tmp_path / "plugins" / "cost-estimate")
    from hermes_cli.web_server_dashboard import _discover_dashboard_plugins

    found = [p for p in _discover_dashboard_plugins() if p.get("name") == "cost-estimate"]
    assert len(found) == 1 and found[0]["source"] == "user" and found[0]["has_api"]
    assert found[0]["tab"]["path"] == "/cost-estimate"
