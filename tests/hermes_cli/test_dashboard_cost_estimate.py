"""The dashboard cost card uses its own authenticated, profile-scoped route."""

from pathlib import Path

from starlette.testclient import TestClient


def test_cost_estimate_route_reads_each_profile_and_requires_auth(tmp_path, monkeypatch):
    from hermes_state import SessionDB
    from hermes_cli import web_server as ws

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    launch = tmp_path / ".hermes"
    worker = launch / "profiles" / "worker"
    monkeypatch.setenv("HERMES_HOME", str(launch))
    for home, amount in [(launch, 12), (worker, 34)]:
        home.mkdir(parents=True, exist_ok=True)
        (home / "config.yaml").write_text("model: {}\n", encoding="utf-8")
        db = SessionDB(db_path=home / "state.db")
        try:
            db.create_session("receipt", source="cli", model="unpriced-test-model")
            db.update_token_counts("receipt", input_tokens=amount, output_tokens=2)
        finally:
            db.close()

    client = TestClient(ws.app)
    url = "/api/analytics/cost-estimate?window=1h"
    assert client.get(url).status_code == 401
    client.headers[ws._SESSION_HEADER_NAME] = ws._SESSION_TOKEN
    for query, amount in [("", 12), ("&profile=worker", 34), ("", 12)]:
        response = client.get(url + query)
        assert response.status_code == 200
        result = response.json()
        assert result["models"][0]["tokens"]["input"] == amount
        assert result["has_unpriced_models"] is True
    assert client.get("/api/analytics/cost-estimate?window=invalid").status_code == 422
