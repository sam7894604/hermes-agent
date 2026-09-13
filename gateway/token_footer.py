"""Build the per-message token-breakdown footer line for bot replies.

Gated per-session by the ``/tokens`` toggle (see the gateway's
``_tokens_display`` map). The numbers are the turn's canonical usage as the
provider reported it — recorded by
:func:`agent.turn_usage.record_response_usage` onto ``agent._last_turn_usage``
and surfaced on the turn result as ``last_turn_usage`` — i.e. the same counts
the compressor and cost accounting use, not a separately derived per-message
tally.
"""
from __future__ import annotations

from typing import Any, Dict


def _int(value: Any) -> int:
    """Non-negative int, or 0 for missing/garbage (footers never raise)."""
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def build_token_line(agent_result: Dict[str, Any]) -> str:
    """Return a compact one-line token breakdown, or "" when unavailable.

    Wrapped in inline-code backticks so it reads as a discreet information
    block appended to the reply tail (renders as monospace on Telegram /
    Discord / Slack etc.; harmless literal backticks on plain-text platforms).
    Magnitudes use :func:`agent.usage_pricing.format_token_count_compact`.
    Example: ```📊 in:1.52K out:234 rsn:128 cache:890```.
    """
    from agent.usage_pricing import format_token_count_compact as _f

    usage = agent_result.get("last_turn_usage")
    if not isinstance(usage, dict):
        usage = {}

    in_tokens = _int(usage.get("prompt_tokens"))
    out_tokens = _int(usage.get("output_tokens") or usage.get("completion_tokens"))
    reason_tokens = _int(usage.get("reasoning_tokens"))
    cache_tokens = _int(usage.get("cache_read_tokens"))

    # Turns that never reached a provider response carry ``last_turn_usage=None``
    # by contract; the agent still reports the prompt size it measured.
    if not in_tokens:
        in_tokens = _int(agent_result.get("last_prompt_tokens"))

    if not (out_tokens or reason_tokens or in_tokens or cache_tokens):
        return ""

    return (
        f"`📊 in:{_f(in_tokens)} out:{_f(out_tokens)} "
        f"rsn:{_f(reason_tokens)} cache:{_f(cache_tokens)}`"
    )
