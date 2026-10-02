"""groq-cf-stt plugin (fork-plugins/groq-cf-stt): Groq Whisper through a Cloudflare AI Gateway. The
provider sends the gateway bearer with an empty provider key when routed through the gateway, behaves like
the built-in groq backend against api.groq.com, and is reachable through the real STT plugin dispatcher."""
from __future__ import annotations

import shutil
from unittest.mock import MagicMock, patch

import pytest

from tests.fork_plugins._plugin_loader import FORK_PLUGINS, load_fork_plugin

_plugin = load_fork_plugin("groq-cf-stt")
CF_URL = "https://gateway.ai.cloudflare.com/v1/acct/gw/groq"


@pytest.fixture
def sample_wav(tmp_path):
    path = tmp_path / "voice.wav"
    path.write_bytes(b"RIFF\x00\x00\x00\x00WAVEfmt ")
    return str(path)


@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))  # no .env, no config: env vars are the only source
    for name in ("GROQ_API_KEY", "GROQ_BASE_URL", "CF_AIG_TOKEN"):
        monkeypatch.delenv(name, raising=False)


def _client_mock(text="hi"):
    client = MagicMock()
    client.audio.transcriptions.create.return_value = text
    return client


class TestTransport:
    def test_cf_gateway_sends_bearer_and_empty_provider_key(self, clean_env, monkeypatch, sample_wav):
        monkeypatch.setenv("GROQ_BASE_URL", CF_URL)
        monkeypatch.setenv("GROQ_API_KEY", "gsk-test")
        monkeypatch.setenv("CF_AIG_TOKEN", "cfut-fake-token")
        provider = _plugin.GroqCloudflareTranscriptionProvider()
        assert provider.is_available()
        with patch("openai.OpenAI", return_value=_client_mock()) as m:
            result = provider.transcribe(sample_wav, model="whisper-large-v3-turbo", language="zh")
        assert result == {"success": True, "transcript": "hi", "provider": "groq-cf"}
        kw = m.call_args.kwargs
        assert kw["base_url"] == CF_URL
        assert kw["default_headers"] == {"cf-aig-authorization": "Bearer cfut-fake-token"}
        assert kw["api_key"] == ""  # BYOK: the gateway supplies the stored Groq key
        create = m.return_value.audio.transcriptions.create.call_args.kwargs
        assert create["model"] == "whisper-large-v3-turbo" and create["language"] == "zh"
        assert "prompt" not in create and create["response_format"] == "text"
        m.return_value.close.assert_called_once()

    def test_direct_groq_sends_key_and_no_gateway_header(self, clean_env, monkeypatch, sample_wav):
        monkeypatch.setenv("GROQ_API_KEY", "gsk-test")
        monkeypatch.setenv("CF_AIG_TOKEN", "cfut-should-be-ignored")
        provider = _plugin.GroqCloudflareTranscriptionProvider()
        with patch("openai.OpenAI", return_value=_client_mock()) as m:
            assert provider.transcribe(sample_wav)["success"] is True
        kw = m.call_args.kwargs
        assert kw["base_url"] == _plugin.DEFAULT_GROQ_BASE_URL
        assert kw["api_key"] == "gsk-test" and kw["default_headers"] is None

    def test_cf_gateway_works_without_a_local_groq_key(self, clean_env, monkeypatch, sample_wav):
        monkeypatch.setenv("GROQ_BASE_URL", CF_URL)
        monkeypatch.setenv("CF_AIG_TOKEN", "cfut-fake-token")
        provider = _plugin.GroqCloudflareTranscriptionProvider()
        assert provider.is_available()
        with patch("openai.OpenAI", return_value=_client_mock()) as m:
            assert provider.transcribe(sample_wav)["success"] is True
        assert m.call_args.kwargs["default_headers"]["cf-aig-authorization"] == "Bearer cfut-fake-token"

    def test_no_credentials_is_an_error_envelope_not_a_call(self, clean_env, sample_wav):
        provider = _plugin.GroqCloudflareTranscriptionProvider()
        assert provider.is_available() is False
        with patch("openai.OpenAI") as m:
            result = provider.transcribe(sample_wav)
        m.assert_not_called()
        assert result["success"] is False and result["transcript"] == "" and "GROQ_API_KEY" in result["error"]
        assert result["provider"] == "groq-cf"

    def test_openai_only_model_is_corrected_and_prompt_forwarded(self, clean_env, monkeypatch, sample_wav):
        monkeypatch.setenv("GROQ_API_KEY", "gsk-test")
        provider = _plugin.GroqCloudflareTranscriptionProvider()
        with patch("openai.OpenAI", return_value=_client_mock(" spaced ")) as m:
            result = provider.transcribe(sample_wav, model="whisper-1", prompt="vocab hint")
        create = m.return_value.audio.transcriptions.create.call_args.kwargs
        assert create["model"] == _plugin.DEFAULT_MODEL and create["prompt"] == "vocab hint"
        assert result["transcript"] == "spaced"

    def test_sdk_failure_becomes_the_error_envelope(self, clean_env, monkeypatch, sample_wav):
        monkeypatch.setenv("GROQ_API_KEY", "gsk-test")
        client = _client_mock()
        client.audio.transcriptions.create.side_effect = RuntimeError("boom")
        provider = _plugin.GroqCloudflareTranscriptionProvider()
        with patch("openai.OpenAI", return_value=client):
            result = provider.transcribe(sample_wav)
        assert result["success"] is False and "boom" in result["error"]
        client.close.assert_called_once()

    def test_structured_error_response_is_not_a_transcript(self, clean_env, monkeypatch, sample_wav):
        monkeypatch.setenv("GROQ_API_KEY", "gsk-test")
        client = _client_mock()
        client.audio.transcriptions.create.return_value = {"text": None, "error": "quota exceeded"}
        provider = _plugin.GroqCloudflareTranscriptionProvider()
        with patch("openai.OpenAI", return_value=client):
            result = provider.transcribe(sample_wav)
        assert result["success"] is False and "quota exceeded" in result["error"]


def test_register_registers_the_provider_under_its_name():
    seen = []

    class _Ctx:
        def register_transcription_provider(self, provider):
            seen.append(provider)

    _plugin.register(_Ctx())
    (provider,) = seen
    assert provider.name == "groq-cf" and provider.default_model() == _plugin.DEFAULT_MODEL
    assert [m["id"] for m in provider.list_models()][0] == _plugin.DEFAULT_MODEL


def test_user_dir_plugin_is_dispatched_by_the_stt_plugin_router(tmp_path, monkeypatch, sample_wav):
    import hermes_yaml as yaml

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    for name in ("GROQ_API_KEY", "GROQ_BASE_URL", "CF_AIG_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GROQ_BASE_URL", CF_URL)
    monkeypatch.setenv("CF_AIG_TOKEN", "cfut-fake-token")
    shutil.copytree(FORK_PLUGINS / "groq-cf-stt", tmp_path / "plugins" / "groq-cf-stt")
    stt_config = {"enabled": True, "provider": "groq-cf", "groq-cf": {"language": "zh", "model": "whisper-large-v3"}}
    (tmp_path / "config.yaml").write_text(yaml.safe_dump({"plugins": {"enabled": ["groq-cf-stt"]}, "stt": stt_config}))

    from hermes_cli import plugins as pmod
    from agent import transcription_registry
    mgr = pmod.PluginManager()
    mgr.discover_and_load()
    assert mgr._plugins["groq-cf-stt"].enabled, mgr._plugins["groq-cf-stt"].error
    assert transcription_registry.get_provider("groq-cf") is not None

    from tools.transcription_command import _dispatch_to_plugin_provider
    with patch("openai.OpenAI", return_value=_client_mock("你好")) as m:
        result = _dispatch_to_plugin_provider(
            sample_wav, "groq-cf", stt_config, model=stt_config["groq-cf"]["model"], language="zh")
    assert result == {"success": True, "transcript": "你好", "provider": "groq-cf"}
    assert m.call_args.kwargs["default_headers"] == {"cf-aig-authorization": "Bearer cfut-fake-token"}
    assert m.return_value.audio.transcriptions.create.call_args.kwargs["model"] == "whisper-large-v3"
