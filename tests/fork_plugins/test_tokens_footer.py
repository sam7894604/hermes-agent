"""tokens-footer plugin (fork-plugins/tokens-footer): the fork's ``tokens`` runtime-footer field as hooks.
``post_api_request`` keeps the latest usage of a session's turn; ``transform_llm_output`` appends the line
when the gateway's resolved footer config lists ``tokens``."""
from __future__ import annotations

import shutil

import pytest

from tests.fork_plugins._plugin_loader import FORK_PLUGINS, load_fork_plugin

_plugin = load_fork_plugin("tokens-footer")
_real_footer_wants_tokens = _plugin.footer_wants_tokens  # captured before the autouse stub below


def _usage(prompt=0, output=0, reasoning=0, cache_read=0):
    """Shape of ``agent._usage_summary_for_api_request_hook`` (CanonicalUsage + prompt/total)."""
    return {"prompt_tokens": prompt, "completion_tokens": output, "total_tokens": prompt + output,
            "input_tokens": max(0, prompt - cache_read), "output_tokens": output,
            "cache_read_tokens": cache_read, "cache_write_tokens": 0, "reasoning_tokens": reasoning,
            "request_count": 1}


@pytest.fixture(autouse=True)
def _fresh_state(monkeypatch):
    _plugin._TURN_USAGE.clear()
    monkeypatch.setattr(_plugin, "footer_wants_tokens", lambda platform: True)


class TestFormat:
    def test_uses_the_shared_compact_magnitudes(self):
        from agent.usage_pricing import format_token_count_compact as compact

        line = _plugin.format_tokens(_usage(prompt=1520, output=234, reasoning=128, cache_read=890))
        assert line == f"in:{compact(1520)} out:{compact(234)} rsn:{compact(128)} cache:{compact(890)}"

    def test_prompt_fallback_empty_and_garbage(self):
        assert _plugin.format_tokens(None, 999) == "in:999 out:0 rsn:0 cache:0"
        assert _plugin.format_tokens(_usage()) == ""
        bad = {"prompt_tokens": "nope", "output_tokens": None, "reasoning_tokens": -5, "cache_read_tokens": 7}
        assert _plugin.format_tokens(bad) == "in:0 out:0 rsn:0 cache:7"


class TestHooks:
    def test_latest_call_of_the_turn_wins_and_is_appended_once(self):
        _plugin._on_post_api_request(session_id="s1", turn_id="t1", usage=_usage(prompt=10, output=1))
        _plugin._on_post_api_request(session_id="s1", turn_id="t1", usage=_usage(prompt=1520, output=234, reasoning=128, cache_read=890))
        out = _plugin._on_transform_llm_output(response_text="Done.\n", session_id="s1", platform="telegram", turn_id="t1")
        assert out == "Done.\n\nin:1.52K out:234 rsn:128 cache:890"
        # consumed: a second transform of the same session has nothing to append
        assert _plugin._on_transform_llm_output(response_text="Done.", session_id="s1", platform="telegram", turn_id="t1") is None

    def test_sessions_are_independent(self):
        _plugin._on_post_api_request(session_id="a", turn_id="t", usage=_usage(prompt=5, output=1))
        _plugin._on_post_api_request(session_id="b", turn_id="t", usage=_usage(prompt=7, output=2))
        assert _plugin._on_transform_llm_output(response_text="x", session_id="b", platform="discord", turn_id="t").endswith("in:7 out:2 rsn:0 cache:0")
        assert _plugin._on_transform_llm_output(response_text="x", session_id="a", platform="discord", turn_id="t").endswith("in:5 out:1 rsn:0 cache:0")

    def test_usage_of_another_turn_is_not_reused(self):
        _plugin._on_post_api_request(session_id="s", turn_id="old", usage=_usage(prompt=5, output=1))
        assert _plugin._on_transform_llm_output(response_text="x", session_id="s", platform="telegram", turn_id="new") is None

    def test_nothing_without_usage_text_or_opt_in(self, monkeypatch):
        assert _plugin._on_transform_llm_output(response_text="x", session_id="s", platform="telegram", turn_id="t") is None
        _plugin._on_post_api_request(session_id="s", turn_id="t", usage=_usage(prompt=5, output=1))
        assert _plugin._on_transform_llm_output(response_text="", session_id="s", platform="telegram", turn_id="t") is None
        monkeypatch.setattr(_plugin, "footer_wants_tokens", lambda platform: False)
        assert _plugin._on_transform_llm_output(response_text="x", session_id="s", platform="telegram", turn_id="t") is None
        assert "s" in _plugin._TURN_USAGE  # not consumed: the footer is simply off for this platform

    def test_empty_usage_leaves_the_text_alone(self):
        _plugin._on_post_api_request(session_id="s", turn_id="t", usage=_usage())
        assert _plugin._on_transform_llm_output(response_text="x", session_id="s", platform="telegram", turn_id="t") is None

    def test_bounded_memory(self):
        for i in range(_plugin._MAX_SESSIONS + 50):
            _plugin._on_post_api_request(session_id=f"s{i}", turn_id="t", usage=_usage(prompt=1))
        assert len(_plugin._TURN_USAGE) == _plugin._MAX_SESSIONS
        assert "s0" not in _plugin._TURN_USAGE and f"s{_plugin._MAX_SESSIONS + 49}" in _plugin._TURN_USAGE


class TestFooterConfig:
    """``footer_wants_tokens`` is upstream's per-platform resolution (``display.runtime_footer`` <
    ``display.platforms.<p>.runtime_footer``) gated on a live gateway process."""

    def _gateway(self, monkeypatch, config):
        import gateway.run as run

        monkeypatch.setattr(run, "_gateway_runner_ref", lambda: object())
        monkeypatch.setattr(run, "_load_gateway_config", lambda: config)
        monkeypatch.setattr(_plugin, "footer_wants_tokens", _real_footer_wants_tokens)

    def test_opt_in_per_platform(self, monkeypatch):
        self._gateway(monkeypatch, {"display": {
            "runtime_footer": {"enabled": True, "fields": ["model", "context_pct", "tokens"]},
            "platforms": {"discord": {"runtime_footer": {"fields": ["model"]}}},
        }})
        assert _plugin.footer_wants_tokens("telegram") is True
        assert _plugin.footer_wants_tokens("discord") is False  # platform override dropped the field

    def test_disabled_footer_or_default_fields_mean_no_line(self, monkeypatch):
        self._gateway(monkeypatch, {"display": {"runtime_footer": {"enabled": False, "fields": ["tokens"]}}})
        assert _plugin.footer_wants_tokens("telegram") is False
        self._gateway(monkeypatch, {"display": {"runtime_footer": {"enabled": True}}})
        assert _plugin.footer_wants_tokens("telegram") is False  # tokens is opt-in, never in the default set

    def test_not_in_a_gateway_process(self, monkeypatch):
        import gateway.run as run

        monkeypatch.setattr(run, "_gateway_runner_ref", lambda: None)
        monkeypatch.setattr(run, "_load_gateway_config", lambda: {"display": {"runtime_footer": {"enabled": True, "fields": ["tokens"]}}})
        monkeypatch.setattr(_plugin, "footer_wants_tokens", _real_footer_wants_tokens)
        assert _plugin.footer_wants_tokens("cli") is False


def test_register_wires_both_hooks():
    class _Ctx:
        hooks = []

        def register_hook(self, name, cb):
            self.hooks.append((name, cb))

    ctx = _Ctx()
    _plugin.register(ctx)
    assert ctx.hooks == [("post_api_request", _plugin._on_post_api_request),
                         ("transform_llm_output", _plugin._on_transform_llm_output)]


def test_user_dir_copy_is_discovered_when_enabled(tmp_path, monkeypatch):
    import hermes_yaml as yaml

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    shutil.copytree(FORK_PLUGINS / "tokens-footer", tmp_path / "plugins" / "tokens-footer")
    (tmp_path / "config.yaml").write_text(yaml.safe_dump({"plugins": {"enabled": ["tokens-footer"]}}))
    from hermes_cli import plugins as pmod

    mgr = pmod.PluginManager()
    mgr.discover_and_load()
    loaded = mgr._plugins["tokens-footer"]
    assert loaded.enabled, loaded.error
    names = {getattr(getattr(rec, "callback", rec), "__name__", "") for hook in ("post_api_request", "transform_llm_output")
             for rec in mgr._hooks.get(hook, [])}
    assert {"_on_post_api_request", "_on_transform_llm_output"} <= names
