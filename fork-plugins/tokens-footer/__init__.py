"""tokens-footer — the turn's token counts appended to gateway replies (the fork's ``tokens`` field).

upstream's runtime footer (``display.runtime_footer``) renders ``model``/``context_pct``/``cwd``/
``latency``/``served_model`` and ignores field names it doesn't know. This plugin keeps the fork's
configuration contract — list ``tokens`` in ``display.runtime_footer.fields`` (or the per-platform
``display.platforms.<p>.runtime_footer`` override) with ``enabled: true`` — and appends
``in:1.52K out:234 rsn:128 cache:890`` for the turn's final API call, right where the fork rendered it,
on the line above upstream's footer.

Two hooks: ``post_api_request`` records the latest provider-reported usage per session turn (the
last call of a tool-using turn, exactly what the fork showed), and ``transform_llm_output`` appends
the line to the final text. A transformed final is edited into an already streamed message by the
gateway (``gateway/run_turn.py``, "Transformed after streaming"), so streamed turns get it too.
Gateway-only: nothing happens in a CLI/TUI process, where upstream's footer never renders either.
"""
from __future__ import annotations

import logging
from collections import OrderedDict
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

FIELD = "tokens"
_MAX_SESSIONS = 512
# session_id -> (turn_id, usage summary of the latest API call of that turn)
_TURN_USAGE: "OrderedDict[str, Tuple[str, Dict[str, Any]]]" = OrderedDict()


def _int(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def format_tokens(usage: Optional[dict], prompt_fallback: int = 0) -> str:
    """``in:1.52K out:234 rsn:128 cache:890`` from a canonical usage dict (``prompt_tokens``,
    ``output_tokens``/``completion_tokens``, ``reasoning_tokens``, ``cache_read_tokens``), with the same
    compact magnitudes the CLI status bar and ``/usage`` print. "" when there is nothing to show;
    garbage values never raise (footers are decoration, not accounting)."""
    from agent.usage_pricing import format_token_count_compact as _f

    usage = usage if isinstance(usage, dict) else {}
    in_tokens = _int(usage.get("prompt_tokens")) or _int(prompt_fallback)
    out_tokens = _int(usage.get("output_tokens") or usage.get("completion_tokens"))
    reason_tokens = _int(usage.get("reasoning_tokens"))
    cache_tokens = _int(usage.get("cache_read_tokens"))
    if not (in_tokens or out_tokens or reason_tokens or cache_tokens):
        return ""
    return f"in:{_f(in_tokens)} out:{_f(out_tokens)} rsn:{_f(reason_tokens)} cache:{_f(cache_tokens)}"


def footer_wants_tokens(platform: str) -> bool:
    """True when this is a gateway process and the resolved runtime-footer config for ``platform``
    is enabled with ``tokens`` among its fields (upstream's own per-platform resolution)."""
    try:
        from gateway.run import _gateway_runner_ref, _load_gateway_config, _platform_config_key
        from gateway.runtime_footer import resolve_footer_config
    except Exception:
        return False
    if _gateway_runner_ref() is None:
        return False
    try:
        try:  # the hook hands over the platform NAME; upstream keys the override by Platform enum
            from gateway.config import Platform
            key = _platform_config_key(Platform(platform)) if platform else None
        except Exception:
            key = platform or None
        cfg = resolve_footer_config(_load_gateway_config(), key)
    except Exception:
        logger.debug("tokens-footer: footer config unavailable", exc_info=True)
        return False
    return bool(cfg.get("enabled")) and FIELD in [str(f) for f in (cfg.get("fields") or [])]


def _on_post_api_request(session_id: str = "", turn_id: str = "", usage: Any = None, **_: Any) -> None:
    if not session_id or not isinstance(usage, dict):
        return None
    _TURN_USAGE[session_id] = (turn_id or "", dict(usage))
    _TURN_USAGE.move_to_end(session_id)
    while len(_TURN_USAGE) > _MAX_SESSIONS:
        _TURN_USAGE.popitem(last=False)
    return None


def _on_transform_llm_output(
    response_text: str = "", session_id: str = "", platform: str = "", turn_id: str = "", **_: Any,
) -> Optional[str]:
    if not response_text or not session_id:
        return None
    record = _TURN_USAGE.get(session_id)
    if record is None:
        return None
    recorded_turn, usage = record
    if turn_id and recorded_turn and recorded_turn != turn_id:
        return None  # usage of an earlier turn that never produced a final text; not this turn's
    if not footer_wants_tokens(platform):
        return None
    line = format_tokens(usage)
    if not line:
        return None
    _TURN_USAGE.pop(session_id, None)
    return f"{response_text.rstrip()}\n\n{line}"


def register(ctx) -> None:
    ctx.register_hook("post_api_request", _on_post_api_request)
    ctx.register_hook("transform_llm_output", _on_transform_llm_output)
