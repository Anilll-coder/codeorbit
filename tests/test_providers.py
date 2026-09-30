"""Tests for the pluggable chat backends behind `codeorbit agent --provider`.

Same discipline as test_agent.py: no real network call. What is pinned is the
translation - the generic message shape `agent.ask_once` builds in and out of
each provider's own wire format, and the credential rules (env only, and a
local OpenAI-compatible server must work with no key at all).
"""
from __future__ import annotations

import pytest

from codeorbit import providers


class Resp:
    """A minimal stand-in for requests.Response."""
    def __init__(self, status=200, body=None, text=""):
        self.status_code = status
        self._body = body or {}
        self.text = text

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise AssertionError(f"unexpected status {self.status_code}")


# ------------------------------------------------------------------ resolve

def test_resolve_reads_the_key_from_the_environment_only(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-123")
    prov = providers.resolve("openai")
    assert prov.api_key == "sk-test-123"
    assert prov.base_url == providers.DEFAULT_OPENAI_BASE


def test_resolve_defaults_to_no_key_when_unset(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    prov = providers.resolve("openai")
    assert prov.api_key is None, "a local server with no key must still work"


def test_resolve_honours_a_custom_base_url():
    prov = providers.resolve("openai", base_url="http://localhost:1234/v1")
    assert prov.base_url == "http://localhost:1234/v1"


def test_resolve_strips_a_trailing_slash():
    prov = providers.resolve("openai", base_url="http://localhost:1234/v1/")
    assert prov.base_url == "http://localhost:1234/v1"


def test_resolve_honours_a_custom_key_env_var(monkeypatch):
    monkeypatch.setenv("MY_CUSTOM_KEY", "sk-custom")
    prov = providers.resolve("openai", api_key_env="MY_CUSTOM_KEY")
    assert prov.api_key == "sk-custom"


def test_resolve_rejects_ollama():
    """Ollama is agent.py's own functions, called directly - routing it
    through here too would give two places a chance to disagree."""
    with pytest.raises(ValueError):
        providers.resolve("ollama")


def test_anthropic_defaults_to_its_own_base_url(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    prov = providers.resolve("anthropic")
    assert prov.base_url == providers.DEFAULT_ANTHROPIC_BASE


# --------------------------------------------------------- missing-key hint

def test_openai_with_no_key_is_not_fatal(monkeypatch):
    """The same code path serves a local server that takes no key at all -
    refusing to start on principle there breaks the no-keys, local-first
    case this project cares about most."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert providers.key_missing_hint("openai") is None


def test_anthropic_with_no_key_is_fatal_and_says_how_to_fix_it(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    hint = providers.key_missing_hint("anthropic")
    assert hint is not None
    assert "ANTHROPIC_API_KEY" in hint
    assert "console.anthropic.com" in hint


def test_anthropic_with_a_key_set_has_no_hint(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-real")
    assert providers.key_missing_hint("anthropic") is None


# ------------------------------------------------------------- tool pairing

def test_tool_calls_pair_with_the_results_that_follow_in_order():
    """Neither Ollama's shape nor this project's own message-building carries
    a call id through the round trip, so both translators re-pair by
    position. This is the shared logic they both depend on."""
    messages = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"function": {"name": "a", "arguments": {}}},
            {"function": {"name": "b", "arguments": {}}},
        ]},
        {"role": "tool", "content": "result a", "tool_name": "a"},
        {"role": "tool", "content": "result b", "tool_name": "b"},
    ]
    pairs = providers._pair_tool_calls(messages)
    assert pairs[0] is None and pairs[1] is None and pairs[2] is None
    assert pairs[3] == (2, 0, messages[2]["tool_calls"][0])
    assert pairs[4] == (2, 1, messages[2]["tool_calls"][1])


def test_pairing_resets_across_a_second_round():
    messages = [
        {"role": "assistant", "content": "", "tool_calls": [
            {"function": {"name": "a", "arguments": {}}}]},
        {"role": "tool", "content": "r1", "tool_name": "a"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"function": {"name": "b", "arguments": {}}}]},
        {"role": "tool", "content": "r2", "tool_name": "b"},
    ]
    pairs = providers._pair_tool_calls(messages)
    assert pairs[1][0] == 0 and pairs[3][0] == 2, "each round pairs with its own assistant turn"


# ------------------------------------------------------- openai translation

def test_openai_chat_parses_a_tool_call(monkeypatch):
    def fake_post(url, headers=None, json=None, timeout=None):
        assert url.endswith("/chat/completions")
        assert "Authorization" not in headers, "no key set: no header at all"
        return Resp(body={"choices": [{
            "message": {"content": "", "tool_calls": [
                {"function": {"name": "codeorbit_search",
                              "arguments": '{"term": "Engine"}'}}]},
            "finish_reason": "tool_calls",
        }]})

    monkeypatch.setattr(providers.requests, "post", fake_post)
    out = providers._openai_chat("http://x/v1", None, "m", [
        {"role": "user", "content": "find Engine"}], [], 8192, 700, 60)

    assert out["tool_calls"] == [
        {"function": {"name": "codeorbit_search", "arguments": {"term": "Engine"}}}]
    assert out["_truncated"] is False


def test_openai_chat_sends_a_bearer_header_when_a_key_is_set(monkeypatch):
    seen = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        seen["headers"] = headers
        return Resp(body={"choices": [{"message": {"content": "hi"},
                                       "finish_reason": "stop"}]})

    monkeypatch.setattr(providers.requests, "post", fake_post)
    providers._openai_chat("http://x/v1", "sk-test", "m", [], [], 8192, 700, 60)
    assert seen["headers"]["Authorization"] == "Bearer sk-test"


def test_openai_chat_flags_length_truncation(monkeypatch):
    def fake_post(*a, **k):
        return Resp(body={"choices": [{"message": {"content": "cut off"},
                                       "finish_reason": "length"}]})
    monkeypatch.setattr(providers.requests, "post", fake_post)
    out = providers._openai_chat("http://x/v1", None, "m", [], [], 8192, 700, 60)
    assert out["_truncated"] is True


def test_openai_chat_raises_providererror_on_http_failure(monkeypatch):
    def fake_post(*a, **k):
        return Resp(status=500, text="boom")
    monkeypatch.setattr(providers.requests, "post", fake_post)
    with pytest.raises(providers.ProviderError):
        providers._openai_chat("http://x/v1", None, "m", [], [], 8192, 700, 60)


def test_openai_chat_wraps_a_connection_failure(monkeypatch):
    import requests as real_requests

    def boom(*a, **k):
        raise real_requests.RequestException("refused")
    monkeypatch.setattr(providers.requests, "post", boom)
    with pytest.raises(providers.ProviderError, match="refused"):
        providers._openai_chat("http://x/v1", None, "m", [], [], 8192, 700, 60)


def test_openai_tool_message_gets_the_matching_call_id(monkeypatch):
    seen = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        seen["messages"] = json["messages"]
        return Resp(body={"choices": [{"message": {"content": "ok"},
                                       "finish_reason": "stop"}]})

    monkeypatch.setattr(providers.requests, "post", fake_post)
    messages = [
        {"role": "assistant", "content": "", "tool_calls": [
            {"function": {"name": "search", "arguments": {"term": "x"}}}]},
        {"role": "tool", "content": "found it", "tool_name": "search"},
    ]
    providers._openai_chat("http://x/v1", None, "m", messages, [], 8192, 700, 60)
    sent = seen["messages"]
    assistant_id = sent[0]["tool_calls"][0]["id"]
    assert sent[1]["role"] == "tool"
    assert sent[1]["tool_call_id"] == assistant_id


# ----------------------------------------------------- anthropic translation

def test_anthropic_messages_extracts_the_system_prompt():
    system, out = providers._anthropic_messages(
        [{"role": "system", "content": "be terse"},
         {"role": "user", "content": "hi"}],
        [None, None],
    )
    assert system == "be terse"
    assert out == [{"role": "user", "content": "hi"}]


def test_anthropic_messages_builds_tool_use_and_tool_result_blocks():
    messages = [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"function": {"name": "search", "arguments": {"term": "x"}}}]},
        {"role": "tool", "content": "found it", "tool_name": "search"},
    ]
    pairs = providers._pair_tool_calls(messages)
    system, out = providers._anthropic_messages(messages, pairs)

    assistant = out[1]
    assert assistant["role"] == "assistant"
    use_block = next(b for b in assistant["content"] if b["type"] == "tool_use")
    assert use_block["name"] == "search"
    assert use_block["input"] == {"term": "x"}

    result_turn = out[2]
    assert result_turn["role"] == "user"
    result_block = result_turn["content"][0]
    assert result_block["type"] == "tool_result"
    assert result_block["tool_use_id"] == use_block["id"]
    assert result_block["content"] == "found it"


def test_anthropic_bundles_multiple_tool_results_into_one_user_turn():
    """Anthropic rejects a request with a tool_use block that has no matching
    result in the very next turn - so two calls in one round must produce ONE
    following user message with two tool_result blocks, not two messages."""
    messages = [
        {"role": "assistant", "content": "", "tool_calls": [
            {"function": {"name": "a", "arguments": {}}},
            {"function": {"name": "b", "arguments": {}}},
        ]},
        {"role": "tool", "content": "ra", "tool_name": "a"},
        {"role": "tool", "content": "rb", "tool_name": "b"},
    ]
    pairs = providers._pair_tool_calls(messages)
    _, out = providers._anthropic_messages(messages, pairs)
    assert len(out) == 2, "one assistant turn, one bundled user turn"
    assert len(out[1]["content"]) == 2


def test_anthropic_tools_use_input_schema_not_parameters():
    tools = [{"type": "function", "function": {
        "name": "codeorbit_search", "description": "find symbols",
        "parameters": {"type": "object", "properties": {"term": {"type": "string"}}},
    }}]
    out = providers._anthropic_tools(tools)
    assert out == [{"name": "codeorbit_search", "description": "find symbols",
                    "input_schema": {"type": "object",
                                     "properties": {"term": {"type": "string"}}}}]


def test_anthropic_chat_parses_text_and_tool_use(monkeypatch):
    def fake_post(url, headers=None, json=None, timeout=None):
        assert url.endswith("/messages")
        assert headers["x-api-key"] == "sk-ant-x"
        assert headers["anthropic-version"]
        return Resp(body={
            "content": [
                {"type": "text", "text": "let me check"},
                {"type": "tool_use", "id": "t1", "name": "codeorbit_search",
                 "input": {"term": "Engine"}},
            ],
            "stop_reason": "tool_use",
        })

    monkeypatch.setattr(providers.requests, "post", fake_post)
    out = providers._anthropic_chat("http://x", "sk-ant-x", "claude-x",
                                    [{"role": "user", "content": "find Engine"}],
                                    [], 700, 60)
    assert out["content"] == "let me check"
    assert out["tool_calls"] == [
        {"function": {"name": "codeorbit_search", "arguments": {"term": "Engine"}}}]
    assert out["_truncated"] is False


def test_anthropic_chat_flags_max_tokens_as_truncated(monkeypatch):
    def fake_post(*a, **k):
        return Resp(body={"content": [{"type": "text", "text": "cut off"}],
                          "stop_reason": "max_tokens"})
    monkeypatch.setattr(providers.requests, "post", fake_post)
    out = providers._anthropic_chat("http://x", "k", "m", [], [], 700, 60)
    assert out["_truncated"] is True


def test_anthropic_supports_tools_true_when_it_calls_the_probe(monkeypatch):
    def fake_post(*a, **k):
        return Resp(body={"content": [{"type": "tool_use", "id": "t1",
                                       "name": "ping", "input": {}}]})
    monkeypatch.setattr(providers.requests, "post", fake_post)
    ok, _ = providers._anthropic_supports_tools("http://x", "k", "m", 30)
    assert ok is True


def test_anthropic_supports_tools_false_when_it_answers_in_prose(monkeypatch):
    def fake_post(*a, **k):
        return Resp(body={"content": [{"type": "text",
                                       "text": "status: all good"}]})
    monkeypatch.setattr(providers.requests, "post", fake_post)
    ok, reply = providers._anthropic_supports_tools("http://x", "k", "m", 30)
    assert ok is False
    assert "all good" in reply


# --------------------------------------------------------------- Provider

def test_provider_chat_dispatches_by_id(monkeypatch):
    calls = []
    monkeypatch.setattr(providers, "_openai_chat",
                        lambda *a, **k: calls.append("openai") or {})
    monkeypatch.setattr(providers, "_anthropic_chat",
                        lambda *a, **k: calls.append("anthropic") or {})

    providers.Provider("openai", "http://x", None).chat("m", [], [], 8192, 700)
    providers.Provider("anthropic", "http://x", "k").chat("m", [], [], 8192, 700)
    assert calls == ["openai", "anthropic"]


def test_provider_chat_matches_the_shape_ask_once_expects(monkeypatch):
    """This is the contract agent.ask_once actually depends on - not any
    particular provider's wire format."""
    def fake_post(*a, **k):
        return Resp(body={"choices": [{"message": {"content": "hello"},
                                       "finish_reason": "stop"}]})
    monkeypatch.setattr(providers.requests, "post", fake_post)

    out = providers.Provider("openai", "http://x/v1", None).chat(
        "m", [{"role": "user", "content": "hi"}], [], 8192, 700)
    assert set(out) >= {"content", "tool_calls", "_truncated"}
    assert isinstance(out["tool_calls"], list)


def test_model_hints_are_provided_for_both_remote_providers():
    for pid in ("openai", "anthropic"):
        prov = providers.Provider(pid, "http://x", None)
        assert prov.model_hints(), f"{pid} should suggest at least one model"
