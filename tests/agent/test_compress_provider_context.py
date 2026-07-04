"""Tests for memory-provider on_pre_compress() context flowing into the summary.

The compaction path calls ``memory_manager.on_pre_compress(messages)`` and now
threads its returned free text through ``compress(provider_context=...)`` into
``_generate_summary``, which injects it verbatim into the summarizer prompt.
Previously the return value was discarded (the hook ran but its output was
dropped). These tests pin the new behaviour.
"""

from unittest.mock import MagicMock, patch

from agent.context_compressor import ContextCompressor


def _make_compressor():
    """Create a ContextCompressor with minimal state for testing."""
    compressor = ContextCompressor.__new__(ContextCompressor)
    compressor.protect_first_n = 2
    compressor.protect_last_n = 5
    compressor.tail_token_budget = 20000
    compressor.context_length = 200000
    compressor.threshold_percent = 0.80
    compressor.threshold_tokens = 160000
    compressor.max_summary_tokens = 10000
    compressor.quiet_mode = True
    compressor.compression_count = 0
    compressor.last_prompt_tokens = 0
    compressor._previous_summary = None
    compressor._summary_failure_cooldown_until = 0.0
    compressor.summary_model = None
    compressor.model = "test-model"
    compressor.provider = "test"
    compressor.base_url = "http://localhost"
    compressor.api_key = "test-key"
    compressor.api_mode = "chat_completions"
    return compressor


_PROVIDER_TEXT = "mem4 路由碼：§sys 系統 · §adr 決策；用 mem_route(code) 讀冷區微檔。"


def test_provider_context_injected_into_summary_prompt():
    compressor = _make_compressor()
    compressor._pending_provider_context = _PROVIDER_TEXT
    turns = [
        {"role": "user", "content": "Tell me about the deploy"},
        {"role": "assistant", "content": "Deployed to toothless."},
    ]

    captured = {}

    def mock_call_llm(**kwargs):
        captured["messages"] = kwargs["messages"]
        resp = MagicMock()
        resp.choices = [MagicMock()]
        resp.choices[0].message.content = "## Goal\nUnderstand deploy."
        return resp

    with patch("agent.context_compressor.call_llm", mock_call_llm):
        result = compressor._generate_summary(turns)

    assert result is not None
    prompt_text = captured["messages"][0]["content"]
    assert "MEMORY PROVIDER CONTEXT" in prompt_text
    assert "## Memory Provider Context" in prompt_text
    assert _PROVIDER_TEXT in prompt_text


def test_no_provider_context_no_injection():
    compressor = _make_compressor()
    # No _pending_provider_context set — _generate_summary must tolerate that
    # (getattr default) and inject nothing.
    turns = [
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Hi"},
    ]

    captured = {}

    def mock_call_llm(**kwargs):
        captured["messages"] = kwargs["messages"]
        resp = MagicMock()
        resp.choices = [MagicMock()]
        resp.choices[0].message.content = "## Goal\nGreeting."
        return resp

    with patch("agent.context_compressor.call_llm", mock_call_llm):
        compressor._generate_summary(turns)

    prompt_text = captured["messages"][0]["content"]
    assert "MEMORY PROVIDER CONTEXT" not in prompt_text


def test_empty_provider_context_no_injection():
    compressor = _make_compressor()
    compressor._pending_provider_context = "   "  # whitespace-only → skip
    turns = [
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Hi"},
    ]

    captured = {}

    def mock_call_llm(**kwargs):
        captured["messages"] = kwargs["messages"]
        resp = MagicMock()
        resp.choices = [MagicMock()]
        resp.choices[0].message.content = "## Goal\nGreeting."
        return resp

    with patch("agent.context_compressor.call_llm", mock_call_llm):
        compressor._generate_summary(turns)

    assert "MEMORY PROVIDER CONTEXT" not in captured["messages"][0]["content"]


def test_compress_stores_provider_context_for_generate_summary():
    compressor = _make_compressor()

    captured = {}

    def tracking_generate(turns, **kwargs):
        # _generate_summary reads instance state, not a kwarg — capture it.
        captured["ctx"] = compressor._pending_provider_context
        return "## Goal\nTest."

    compressor._generate_summary = tracking_generate

    messages = [
        {"role": "system", "content": "System prompt"},
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "reply1"},
        {"role": "user", "content": "second"},
        {"role": "assistant", "content": "reply2"},
        {"role": "user", "content": "third"},
        {"role": "assistant", "content": "reply3"},
        {"role": "user", "content": "fourth"},
        {"role": "assistant", "content": "reply4"},
    ]

    compressor.compress(messages, current_tokens=100000, provider_context="PROVIDER-XYZ")

    assert captured["ctx"] == "PROVIDER-XYZ"


def test_compress_defaults_provider_context_empty():
    compressor = _make_compressor()

    captured = {}

    def tracking_generate(turns, **kwargs):
        captured["ctx"] = compressor._pending_provider_context
        return "## Goal\nTest."

    compressor._generate_summary = tracking_generate

    messages = [
        {"role": "system", "content": "System prompt"},
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "reply1"},
        {"role": "user", "content": "second"},
        {"role": "assistant", "content": "reply2"},
        {"role": "user", "content": "third"},
        {"role": "assistant", "content": "reply3"},
        {"role": "user", "content": "fourth"},
        {"role": "assistant", "content": "reply4"},
    ]

    compressor.compress(messages, current_tokens=100000)

    assert captured["ctx"] == ""
