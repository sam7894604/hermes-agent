"""cost-estimate dashboard plugin — backend.

Mounted at ``/api/plugins/cost-estimate/`` by the dashboard plugin system (which also applies the
session-token auth and the ``?profile=`` scope). One route:

    GET /estimate?window=1h|24h|7d|30d

Per-model cost over the window broken down by price tier from each model's published pricing
(``agent.usage_pricing``), with a daily/monthly projection; models without known pricing fall back
to the stored ``estimated_cost_usd`` and are flagged. This used to be a fork route in
``hermes_cli/web_routers/analytics.py`` plus a ``SessionDB`` method; the aggregate query now lives
here, on upstream's pooled read path.
"""
from __future__ import annotations

import asyncio
import importlib.util
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Query

from hermes_cli.web_deps import late
from hermes_cli.web_routers._common import corrupt_store_as_status

_open_session_db_for_profile = late("_open_session_db_for_profile", "hermes_cli.web_server_sessions")
_session_db_path_for_profile = late("_session_db_path_for_profile", "hermes_cli.web_server_sessions")
_profile_scope = late("_profile_scope", "hermes_cli.web_server_profiles")

router = APIRouter()

WINDOWS = {"1h": 3600, "24h": 86400, "7d": 7 * 86400, "30d": 30 * 86400}


def _cost_math():
    """The DB-free pricing math, loaded by path (plugin api modules are imported standalone)."""
    spec = importlib.util.spec_from_file_location("hermes_fork_cost_math", Path(__file__).with_name("cost_math.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def session_cost_aggregates(db, since_ts: float) -> List[Dict[str, Any]]:
    """Per-(model, provider, base_url) token sums for sessions active at/after ``since_ts``
    (``COALESCE(ended_at, started_at)``), plus the stored ``estimated_cost_usd``."""
    rows = db._read_all(
        "SELECT model, billing_provider, billing_base_url, "
        "COUNT(*) AS sessions, "
        "COALESCE(SUM(input_tokens),0) AS input_tokens, "
        "COALESCE(SUM(output_tokens),0) AS output_tokens, "
        "COALESCE(SUM(cache_read_tokens),0) AS cache_read_tokens, "
        "COALESCE(SUM(cache_write_tokens),0) AS cache_write_tokens, "
        "COALESCE(SUM(reasoning_tokens),0) AS reasoning_tokens, "
        "COALESCE(SUM(estimated_cost_usd),0) AS estimated_cost_usd "
        "FROM sessions "
        "WHERE COALESCE(ended_at, started_at) >= ? "
        "GROUP BY model, billing_provider, billing_base_url",
        (since_ts,),
    )
    return [{
        "model": r["model"],
        "billing_provider": r["billing_provider"],
        "billing_base_url": r["billing_base_url"],
        "sessions": int(r["sessions"] or 0),
        "input_tokens": int(r["input_tokens"] or 0),
        "output_tokens": int(r["output_tokens"] or 0),
        "cache_read_tokens": int(r["cache_read_tokens"] or 0),
        "cache_write_tokens": int(r["cache_write_tokens"] or 0),
        "reasoning_tokens": int(r["reasoning_tokens"] or 0),
        "estimated_cost_usd": float(r["estimated_cost_usd"] or 0.0),
    } for r in rows]


def estimate_for_db(db, window: str, now: Optional[float] = None) -> Dict[str, Any]:
    from agent.usage_pricing import get_pricing_entry

    seconds = WINDOWS[window]
    now = time.time() if now is None else now
    groups = session_cost_aggregates(db, now - seconds)

    def lookup(model, provider, base_url):
        return get_pricing_entry(model, provider=provider, base_url=base_url) if model else None

    return {"window": window, "generated_at": now, **_cost_math().compute_cost_estimate(groups, seconds, lookup)}


@router.get("/estimate")
async def get_estimate(
    window: str = Query("24h", pattern="^(1h|24h|7d|30d)$"),
    profile: Optional[str] = None,
):
    def _run():
        with _profile_scope(profile):
            db = _open_session_db_for_profile(profile, read_only=True)
            try:
                return estimate_for_db(db, window)
            finally:
                db.close()

    with corrupt_store_as_status(_session_db_path_for_profile(profile)):
        return await asyncio.to_thread(_run)
