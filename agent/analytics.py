"""Pure analytics computations over session-level usage aggregates.

DB-free so the math is unit-testable in isolation. Inputs come from
:meth:`hermes_state.SessionDB.get_session_cost_aggregates`.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional


def _price(per_million: Any, tokens: int) -> Optional[float]:
    """Cost in USD for ``tokens`` at ``per_million`` USD/1M, or None if unpriced."""
    if per_million is None:
        return None
    return round(float(per_million) * tokens / 1_000_000.0, 6)


def compute_cost_estimate(
    groups: List[Dict[str, Any]],
    window_seconds: int,
    price_lookup,
) -> Dict[str, Any]:
    """Per-model cost broken down by price tier, with daily/monthly projection.

    ``groups``: rows from
    :meth:`hermes_state.SessionDB.get_session_cost_aggregates`.
    ``price_lookup(model, provider, base_url)``: returns a pricing entry with
    ``input_cost_per_million`` / ``output_cost_per_million`` /
    ``cache_read_cost_per_million`` / ``cache_write_cost_per_million`` (or
    None). When a model has no known pricing the stored ``estimated_cost_usd``
    is used as a fallback and flagged.
    """
    models: List[Dict[str, Any]] = []
    total = 0.0
    total_input_cost = total_output_cost = total_cache_cost = 0.0
    any_unpriced = False

    for g in groups:
        entry = None
        try:
            entry = price_lookup(g.get("model"), g.get("billing_provider"), g.get("billing_base_url"))
        except Exception:
            entry = None

        in_cost = out_cost = cr_cost = cw_cost = None
        if entry is not None:
            in_cost = _price(getattr(entry, "input_cost_per_million", None), g["input_tokens"])
            out_cost = _price(getattr(entry, "output_cost_per_million", None), g["output_tokens"])
            cr_cost = _price(getattr(entry, "cache_read_cost_per_million", None), g["cache_read_tokens"])
            cw_cost = _price(getattr(entry, "cache_write_cost_per_million", None), g["cache_write_tokens"])

        priced = any(c is not None for c in (in_cost, out_cost, cr_cost, cw_cost))
        if priced:
            grp_total = round(sum(c or 0.0 for c in (in_cost, out_cost, cr_cost, cw_cost)), 6)
            source = "pricing"
        else:
            grp_total = round(float(g.get("estimated_cost_usd") or 0.0), 6)
            source = "stored_estimate"
            any_unpriced = True

        total += grp_total
        total_input_cost += in_cost or 0.0
        total_output_cost += out_cost or 0.0
        total_cache_cost += (cr_cost or 0.0) + (cw_cost or 0.0)

        models.append({
            "model": g.get("model"),
            "provider": g.get("billing_provider"),
            "sessions": g.get("sessions", 0),
            "tokens": {
                "input": g["input_tokens"], "output": g["output_tokens"],
                "cache_read": g["cache_read_tokens"], "cache_write": g["cache_write_tokens"],
                "reasoning": g["reasoning_tokens"],
            },
            "cost_breakdown": {
                "input": in_cost, "output": out_cost,
                "cache_read": cr_cost, "cache_write": cw_cost,
            },
            "cost_usd": grp_total,
            "cost_source": source,
        })

    models.sort(key=lambda m: m["cost_usd"], reverse=True)
    total = round(total, 6)
    days = max(window_seconds / 86400.0, 1e-9)
    daily = round(total / days, 6)
    return {
        "total_cost_usd": total,
        "cost_by_tier": {
            "input": round(total_input_cost, 6),
            "output": round(total_output_cost, 6),
            "cache": round(total_cache_cost, 6),
        },
        "projection": {"daily_usd": daily, "monthly_usd": round(daily * 30, 6)},
        "has_unpriced_models": any_unpriced,
        "models": models,
    }
