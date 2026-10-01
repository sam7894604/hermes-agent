"""Gateway runtime-metadata footer (model · context % · cwd), off by default to keep replies
minimal. Config: ``display.runtime_footer: {enabled: bool, fields: [model, context_pct, cwd]}``
(order shown; drop any to hide), per-platform override ``display.platforms.<p>.runtime_footer``,
toggled by ``/footer on|off``. Fields: ``model`` (vendor prefix dropped), ``context_pct`` (last-call
occupancy), ``tokens`` (the turn's final-call token counts, opt-in), ``latency`` (turn wall-clock, opt-in — NOT in the default set so an unset ``fields``
renders exactly as before), ``served_model`` (opt-in, ``alias → served``: the deployment a routing
proxy reported via ``x-litellm-model-id`` / ``x-litellm-model-api-base``, or Hermes' own fallback
route; skipped when the served model is the requested one), ``cwd`` (home-relative). ``gateway/run.py`` appends the footer to the
final response only (never to tool-progress or streaming partials); when streaming already
delivered the text, it goes out as a trailing message via ``send_trailing_footer()``."""

from __future__ import annotations

import os
from typing import Any, Iterable, Optional

_DEFAULT_FIELDS: tuple[str, ...] = ("model", "context_pct", "cwd")
_SEP = " · "


def _home_relative_cwd(cwd: str) -> str:
    """Return *cwd* with ``$HOME`` collapsed to ``~``.  Empty string if unset."""
    if not cwd:
        return ""
    try:
        home = os.path.expanduser("~")
        p = os.path.abspath(cwd)
        if home and (p == home or p.startswith(home + os.sep)):
            return "~" + p[len(home):]
        return p
    except Exception:
        return cwd


def _model_short(model: Optional[str]) -> str:
    """Drop ``vendor/`` prefix (``openai/gpt-5.4`` → ``gpt-5.4``)."""
    return model.rsplit("/", 1)[-1] if model else ""


def _env_cwd() -> str:
    try:
        from tools.terminal_scope import terminal_env
    except ImportError:
        return os.environ.get("TERMINAL_CWD", "")
    return terminal_env("TERMINAL_CWD", "")


def resolve_footer_config(user_config: dict[str, Any] | None, platform_key: str | None = None) -> dict[str, Any]:
    """Resolve effective footer config: defaults (enabled=False) <
    ``display.runtime_footer`` < ``display.platforms.<platform_key>.runtime_footer``."""
    resolved = {"enabled": False, "fields": list(_DEFAULT_FIELDS)}
    cfg = (user_config or {}).get("display") or {}
    plat_cfg = (cfg.get("platforms") or {}).get(platform_key) if platform_key else None
    sections = [cfg.get("runtime_footer"), plat_cfg.get("runtime_footer") if isinstance(plat_cfg, dict) else None]
    for section in sections:
        if not isinstance(section, dict):
            continue
        if "enabled" in section:
            resolved["enabled"] = bool(section.get("enabled"))
        if isinstance(section.get("fields"), list) and section["fields"]:
            resolved["fields"] = [str(f) for f in section["fields"]]
    return resolved


def _format_latency(seconds: float) -> str:
    """Humanize a turn duration: ``<1s``, ``22s``, ``1m05s``."""
    if seconds < 1:
        return "<1s"
    total = int(round(seconds))
    if total < 60:
        return f"{total}s"
    m, sec = divmod(total, 60)
    return f"{m}m{sec:02d}s"


def _format_tokens(usage: Optional[dict], prompt_fallback: int) -> str:
    """``in:1.52K out:234 rsn:128 cache:890`` from the turn's canonical usage dict (the provider-
    reported counts ``agent.turn_usage.record_response_usage`` stashes, surfaced on the turn result
    as ``last_turn_usage``). A turn that never reached a provider response carries ``None``; the
    measured prompt size still gets reported. "" when there is nothing to show."""
    from agent.usage_pricing import format_token_count_compact as _f

    def _int(value: Any) -> int:
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            return 0

    usage = usage if isinstance(usage, dict) else {}
    in_tokens = _int(usage.get("prompt_tokens")) or _int(prompt_fallback)
    out_tokens = _int(usage.get("output_tokens") or usage.get("completion_tokens"))
    reason_tokens = _int(usage.get("reasoning_tokens"))
    cache_tokens = _int(usage.get("cache_read_tokens"))
    if not (in_tokens or out_tokens or reason_tokens or cache_tokens):
        return ""
    return f"in:{_f(in_tokens)} out:{_f(out_tokens)} rsn:{_f(reason_tokens)} cache:{_f(cache_tokens)}"


def format_runtime_footer(*, model: Optional[str], context_tokens: int,
                          context_length: Optional[int], cwd: Optional[str] = None,
                          turn_seconds: Optional[float] = None,
                          requested_model: Optional[str] = None, served_model: Optional[str] = None,
                          turn_usage: Optional[dict] = None,
                          fields: Iterable[str] = _DEFAULT_FIELDS) -> str:
    """Render the footer line, or "" if no fields have data. Fields whose data is missing (and
    unknown field names) are skipped silently — a partial footer beats ``?%`` or empty slots."""
    def context_pct() -> str:
        if context_length and context_length > 0 and context_tokens >= 0:
            return f"{max(0, min(100, round((context_tokens / context_length) * 100)))}%"
        return ""

    def served() -> str:
        requested = requested_model or model
        alias = _model_short(requested)
        if served_model and served_model not in (alias, requested):
            return f"{alias} → {served_model}"
        return ""

    renderers = {
        "model": lambda: _model_short(model),
        "served_model": served,
        "context_pct": context_pct,
        # Skipped when the caller did not measure (None) or the value is negative.
        "latency": lambda: _format_latency(turn_seconds) if turn_seconds is not None and turn_seconds >= 0 else "",
        "cwd": lambda: _home_relative_cwd(cwd or _env_cwd()),
        # Per-turn token counts (opt-in): the final API call's usage, as the provider reported it.
        "tokens": lambda: _format_tokens(turn_usage, context_tokens),
    }
    return _SEP.join(v for field in fields if (render := renderers.get(field)) and (v := render()))


def build_footer_line(*, user_config: dict[str, Any] | None, platform_key: str | None,
                      model: Optional[str], context_tokens: int, context_length: Optional[int],
                      cwd: Optional[str] = None, turn_seconds: Optional[float] = None,
                      requested_model: Optional[str] = None, served_model: Optional[str] = None,
                      turn_usage: Optional[dict] = None) -> str:
    """Entry point for gateway/run.py: footer text, or "" when disabled / no data. Callers append it
    to the final response themselves, preserving a single blank line of separation.
    ``turn_seconds`` is the caller-measured (``time.monotonic()``) run duration; ``None`` skips the
    ``latency`` field."""
    cfg = resolve_footer_config(user_config, platform_key)
    if not cfg.get("enabled"):
        return ""
    return format_runtime_footer(model=model, context_tokens=context_tokens,
                                 context_length=context_length, cwd=cwd, turn_seconds=turn_seconds,
                                 requested_model=requested_model, served_model=served_model,
                                 turn_usage=turn_usage, fields=cfg.get("fields") or _DEFAULT_FIELDS)
