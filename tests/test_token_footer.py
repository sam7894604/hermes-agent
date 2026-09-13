"""gateway.token_footer.build_token_line — per-reply token footer.

The footer reads the turn's canonical usage (``last_turn_usage`` on the turn
result, recorded by ``agent.turn_usage.record_response_usage``), which is the
same provider-reported dict the compressor and cost accounting consume.
"""
from agent.usage_pricing import format_token_count_compact
from gateway.token_footer import build_token_line


def _usage(prompt=0, output=0, reasoning=0, cache_read=0):
    """The canonical usage shape record_response_usage stashes."""
    return {
        "prompt_tokens": prompt,
        "completion_tokens": output,
        "total_tokens": prompt + output,
        "input_tokens": max(0, prompt - cache_read),
        "output_tokens": output,
        "cache_read_tokens": cache_read,
        "cache_write_tokens": 0,
        "reasoning_tokens": reasoning,
    }


def test_magnitude_formatting_matches_upstream_compact():
    # The footer no longer ships its own formatter; these are the shapes it relies on.
    assert format_token_count_compact(0) == "0"
    assert format_token_count_compact(234) == "234"
    assert format_token_count_compact(999) == "999"
    assert format_token_count_compact(1000) == "1K"
    assert format_token_count_compact(1520) == "1.52K"
    assert format_token_count_compact(23500) == "23.5K"
    assert format_token_count_compact(123456) == "123K"
    assert format_token_count_compact(1234567) == "1.23M"


def test_full_usage_rendered():
    res = {"last_turn_usage": _usage(prompt=1520, output=234, reasoning=128, cache_read=890)}
    assert build_token_line(res) == "`📊 in:1.52K out:234 rsn:128 cache:890`"


def test_tool_heavy_turn_reports_the_final_api_call():
    # A turn with tool loops: the stashed usage is the LAST provider response,
    # which is the one that produced the reply being decorated.
    res = {"last_turn_usage": _usage(prompt=8000, output=300, reasoning=40, cache_read=6000)}
    assert build_token_line(res) == "`📊 in:8K out:300 rsn:40 cache:6K`"


def test_completion_tokens_alias_used_when_output_missing():
    usage = _usage(prompt=100, output=0)
    usage["output_tokens"] = 0
    usage["completion_tokens"] = 55
    assert build_token_line({"last_turn_usage": usage}) == "`📊 in:100 out:55 rsn:0 cache:0`"


def test_no_usage_falls_back_to_last_prompt_tokens():
    # ``last_turn_usage`` is None by contract when the turn never reached a
    # provider response; the measured prompt size still gets reported.
    res = {"last_turn_usage": None, "last_prompt_tokens": 999}
    assert build_token_line(res) == "`📊 in:999 out:0 rsn:0 cache:0`"


def test_garbage_values_do_not_raise():
    res = {"last_turn_usage": {"prompt_tokens": "nope", "output_tokens": None,
                               "reasoning_tokens": -5, "cache_read_tokens": 7}}
    assert build_token_line(res) == "`📊 in:0 out:0 rsn:0 cache:7`"


def test_empty_returns_blank():
    assert build_token_line({"last_turn_usage": None}) == ""
    assert build_token_line({}) == ""
    assert build_token_line({"last_turn_usage": _usage()}) == ""
