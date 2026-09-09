"""Tests for the local Ollama agent loop.

The model is never called. What is pinned is the machinery around it - the
tool-format conversion, the model-capability gate, and the refusal path - all of
which must behave correctly whether or not Ollama is running or any particular
model is installed. Tests that need a live model would be untestable on CI and
would pass or fail based on which models the machine happens to hold.
"""
from __future__ import annotations

import pytest
from typer.testing import CliRunner

from codeorbit import agent, cli

runner = CliRunner()


@pytest.fixture(autouse=True)
def _reset_shared_path():
    cli._shared["path"] = None
    yield
    cli._shared["path"] = None


class FakeTool:
    def __init__(self, name, description, schema):
        self.name = name
        self.description = description
        self.input_schema = schema


# ------------------------------------------------------- tool conversion

def test_mcp_tools_convert_to_ollama_shape():
    tools = agent.mcp_tools_to_ollama([
        FakeTool("codeorbit_search", "Find symbols by name.",
                 {"type": "object", "properties": {"term": {"type": "string"}},
                  "required": ["term"]}),
    ])
    assert len(tools) == 1
    fn = tools[0]["function"]
    assert tools[0]["type"] == "function"
    assert fn["name"] == "codeorbit_search"
    assert fn["parameters"]["required"] == ["term"]


def test_conversion_survives_a_tool_with_no_schema():
    tools = agent.mcp_tools_to_ollama([FakeTool("t", "d", None)])
    assert tools[0]["function"]["parameters"]["type"] == "object"


def test_long_descriptions_are_capped():
    tools = agent.mcp_tools_to_ollama([FakeTool("t", "x" * 5000, None)])
    assert len(tools[0]["function"]["description"]) <= 1024


# -------------------------------------------------------- capability gate

def test_a_model_that_returns_prose_is_reported_as_incapable(monkeypatch):
    """The failure this guards against is silent: a model with no tool template
    does not error, it fabricates a result and states it as fact."""
    class Resp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self):
            return {"message": {"content": '{"status": "success", "data": {}}'}}

    monkeypatch.setattr(agent.requests, "post", lambda *a, **k: Resp())
    ok, reply = agent.supports_tools("pretend-model")
    assert ok is False
    assert "success" in reply, "the fabricated reply should be surfaced"


def test_a_model_that_emits_tool_calls_is_reported_as_capable(monkeypatch):
    class Resp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self):
            return {"message": {"tool_calls": [
                {"function": {"name": "ping", "arguments": {}}}]}}

    monkeypatch.setattr(agent.requests, "post", lambda *a, **k: Resp())
    ok, _ = agent.supports_tools("pretend-model")
    assert ok is True


def test_an_unreachable_ollama_is_not_a_crash(monkeypatch):
    def boom(*a, **k):
        raise agent.requests.RequestException("connection refused")
    monkeypatch.setattr(agent.requests, "post", boom)
    ok, msg = agent.supports_tools("anything")
    assert ok is False
    assert "failed" in msg.lower()


# ------------------------------------------------------------ suggestions

def test_suggestion_prefers_a_model_already_installed(monkeypatch):
    monkeypatch.setattr(agent, "installed_models", lambda: ["llama3.2:3b"])
    text = agent.suggest_model()
    assert "already installed" in text
    assert "llama3.2:3b" in text


def test_suggestion_offers_a_pull_when_nothing_suitable_is_present(monkeypatch):
    monkeypatch.setattr(agent, "installed_models", lambda: ["phi4-mini:latest"])
    text = agent.suggest_model()
    assert "ollama pull" in text


# ------------------------------------------------------------------- CLI

def test_agent_refuses_a_model_that_cannot_call_tools(monkeypatch, tmp_path):
    monkeypatch.setattr(cli.llm, "available", lambda: True)
    monkeypatch.setattr(agent, "installed_models", lambda: ["phi4-mini:latest"])
    monkeypatch.setattr(agent, "supports_tools",
                        lambda m, **k: (False, "I would call the tool now."))

    r = runner.invoke(cli.app, ["agent", "q", "-m", "phi4-mini", "-p", str(tmp_path)])
    assert r.exit_code == 1
    assert "cannot call tools" in r.output
    assert "ollama pull" in r.output or "already installed" in r.output


def test_agent_reports_when_ollama_is_down(monkeypatch, tmp_path):
    monkeypatch.setattr(cli.llm, "available", lambda: False)
    r = runner.invoke(cli.app, ["agent", "q", "-p", str(tmp_path)])
    assert r.exit_code == 1
    assert "ollama" in r.output.lower()


def test_agent_reports_when_no_capable_model_is_installed(monkeypatch, tmp_path):
    monkeypatch.setattr(cli.llm, "available", lambda: True)
    monkeypatch.setattr(agent, "installed_models", lambda: ["phi4-mini:latest"])
    r = runner.invoke(cli.app, ["agent", "q", "-p", str(tmp_path)])
    assert r.exit_code == 1
    assert "No tool-calling model" in r.output


def test_check_lists_each_model_and_skips_embedders(monkeypatch, tmp_path):
    monkeypatch.setattr(cli.llm, "available", lambda: True)
    monkeypatch.setattr(agent, "installed_models",
                        lambda: ["llama3.2:3b", "nomic-embed-text:latest"])
    monkeypatch.setattr(agent, "supports_tools", lambda m, **k: (True, ""))

    r = runner.invoke(cli.app, ["agent", "x", "--check", "-p", str(tmp_path)])
    assert r.exit_code == 0
    assert "llama3.2" in r.output
    assert "nomic-embed" not in r.output, "embedding models cannot chat; skip them"
