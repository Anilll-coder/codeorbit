"""Tests for the local Ollama agent loop.

The model is never called. What is pinned is the machinery around it - the
tool-format conversion, the model-capability gate, and the refusal path - all of
which must behave correctly whether or not Ollama is running or any particular
model is installed. Tests that need a live model would be untestable on CI and
would pass or fail based on which models the machine happens to hold.
"""
from __future__ import annotations

import re

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


def test_a_stopped_ollama_is_started_rather_than_reported(monkeypatch, tmp_path):
    """Telling someone to open another terminal and run a daemon is a step they
    were always going to take. Take it for them."""
    started = {"called": False}

    def fake_start(timeout=25.0):
        started["called"] = True
        return True

    monkeypatch.setattr(cli.llm, "available", lambda: False)
    monkeypatch.setattr(cli.llm, "installed", lambda: True)
    monkeypatch.setattr(cli.llm, "start_server", fake_start)
    monkeypatch.setattr(agent, "installed_models", lambda: [])
    r = runner.invoke(cli.app, ["agent", "q", "-p", str(tmp_path)])
    assert started["called"], "it should have tried to start Ollama"
    assert "started Ollama" in r.output


def test_a_missing_ollama_says_how_to_install_it(monkeypatch, tmp_path):
    monkeypatch.setattr(cli.llm, "available", lambda: False)
    monkeypatch.setattr(cli.llm, "installed", lambda: False)
    # Must not try to spawn something that is not there.
    monkeypatch.setattr(cli.llm, "start_server",
                        lambda *a, **k: pytest.fail("should not spawn"))
    r = runner.invoke(cli.app, ["agent", "q", "-p", str(tmp_path)])
    assert r.exit_code == 1
    assert "ollama.com" in r.output


def test_an_ollama_that_will_not_start_says_so(monkeypatch, tmp_path):
    monkeypatch.setattr(cli.llm, "available", lambda: False)
    monkeypatch.setattr(cli.llm, "installed", lambda: True)
    monkeypatch.setattr(cli.llm, "start_server", lambda *a, **k: False)
    r = runner.invoke(cli.app, ["agent", "q", "-p", str(tmp_path)])
    assert r.exit_code == 1
    assert "ollama serve" in r.output


def test_start_server_does_not_spawn_when_ollama_is_absent(monkeypatch):
    import subprocess
    monkeypatch.setattr(cli.llm, "available", lambda: False)
    monkeypatch.setattr(cli.llm, "installed", lambda: False)
    monkeypatch.setattr(subprocess, "Popen",
                        lambda *a, **k: pytest.fail("should not spawn"))
    assert cli.llm.start_server(timeout=0.1) is False


def test_start_server_is_a_no_op_when_already_up(monkeypatch):
    import subprocess
    monkeypatch.setattr(cli.llm, "available", lambda: True)
    monkeypatch.setattr(subprocess, "Popen",
                        lambda *a, **k: pytest.fail("already running"))
    assert cli.llm.start_server(timeout=0.1) is True


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


# ------------------------------------------------- a path is not a question

@pytest.mark.parametrize("arg", [".", "..", "codeorbit", "tests"])
def test_a_directory_argument_is_read_as_a_path(arg):
    assert cli._looks_like_a_path(arg) is True


@pytest.mark.parametrize("arg", [
    "what are important symbols in this project?",
    "how does load work",
    "Console.print",
    "is load safe to change?",
    "nonexistent_directory_xyz",
])
def test_a_question_is_never_read_as_a_path(arg):
    assert cli._looks_like_a_path(arg) is False


def test_agent_dot_opens_a_session_instead_of_asking_about_a_dot(monkeypatch, tmp_path):
    """`codeorbit agent .` used to spend a round having the model reply that it
    was ready and waiting for a question."""
    seen = {}

    async def fake_session(agentmod, root, model, rounds, max_tokens):
        seen["root"] = root
        seen["interactive"] = True

    async def fake_once(*a, **k):
        seen["interactive"] = False

    monkeypatch.setattr(cli.llm, "available", lambda: True)
    monkeypatch.setattr(agent, "installed_models", lambda: ["llama3.2:3b"])
    monkeypatch.setattr(agent, "supports_tools", lambda m, **k: (True, ""))
    monkeypatch.setattr(cli, "_agent_session", fake_session)
    monkeypatch.setattr(cli, "_agent_once", fake_once)

    r = runner.invoke(cli.app, ["agent", ".", "-p", str(tmp_path)])
    assert r.exit_code == 0, r.output
    assert seen.get("interactive") is True


def test_a_real_question_still_runs_once(monkeypatch, tmp_path):
    seen = {}

    async def fake_once(agentmod, root, question, model, rounds, max_tokens):
        seen["question"] = question

    monkeypatch.setattr(cli.llm, "available", lambda: True)
    monkeypatch.setattr(agent, "installed_models", lambda: ["llama3.2:3b"])
    monkeypatch.setattr(agent, "supports_tools", lambda m, **k: (True, ""))
    monkeypatch.setattr(cli, "_agent_once", fake_once)

    r = runner.invoke(cli.app, ["agent", "how does load work", "-p", str(tmp_path)])
    assert r.exit_code == 0, r.output
    assert seen.get("question") == "how does load work"


# --------------------------------------------------------- context budget

def test_prune_drops_the_oldest_tool_results_first():
    msgs = [
        {"role": "system", "content": "S"},
        {"role": "user", "content": "Q"},
        {"role": "tool", "content": "A" * 8000},
        {"role": "tool", "content": "B" * 8000},
        {"role": "tool", "content": "C" * 8000},
    ]
    out = agent.prune(msgs, budget=14000)
    assert out[0]["content"] == "S", "the system prompt is never dropped"
    assert out[1]["content"] == "Q", "the question is never dropped"
    assert out[-1]["content"].startswith("C"), "the newest result is kept"
    assert "dropped" in out[2]["content"]
    total = sum(len(m["content"]) for m in out)
    assert total < 14000


def test_prune_leaves_a_small_conversation_alone():
    msgs = [{"role": "system", "content": "S"}, {"role": "user", "content": "Q"}]
    assert agent.prune(msgs) == msgs


def test_history_keeps_prose_and_forgets_tool_output():
    h = agent.remember([], "q1", "a" * 5000)
    assert len(h) == 2
    assert len(h[1]["content"]) == agent.HISTORY_ANSWER_CHARS
    assert all(m["role"] in ("user", "assistant") for m in h)


def test_history_is_capped_so_a_long_session_does_not_slow_down():
    h = []
    for i in range(12):
        h = agent.remember(h, f"q{i}", f"a{i}")
    assert len(h) == agent.HISTORY_TURNS * 2
    assert h[0]["content"] == "q9", "the oldest turns fall off"


def test_tool_results_are_clipped_before_the_model_sees_them():
    assert agent.MAX_TOOL_CHARS < 3000, (
        "a single overview can be thousands of characters, and on a CPU model "
        "every one of them costs time twice")


# ---------------------------------------------------------- truncation

def test_a_severed_answer_is_flagged(monkeypatch):
    """An answer that stops mid-word must not read as a finished one."""
    import requests

    class R:
        status_code = 200
        @staticmethod
        def json():
            return {"message": {"content": "the answer is cut off here and"},
                    "done_reason": "length"}

    monkeypatch.setattr(requests, "post", lambda *a, **k: R())
    msg = agent._chat("m", [], [], 8192, 700)
    assert msg["_truncated"] is True


def test_a_complete_answer_is_not_flagged(monkeypatch):
    import requests

    class R:
        status_code = 200
        @staticmethod
        def json():
            return {"message": {"content": "done."}, "done_reason": "stop"}

    monkeypatch.setattr(requests, "post", lambda *a, **k: R())
    assert agent._chat("m", [], [], 8192, 700)["_truncated"] is False


def test_the_system_prompt_asks_for_brevity():
    """The transcript that prompted this listed fourteen symbols and then ran
    out of tokens partway through the fifteenth."""
    assert "BE BRIEF" in agent.SYSTEM
    assert "6 sentences" in agent.SYSTEM


# ------------------------------------------------------ progress indicator

def test_the_spinner_ticks_while_the_model_is_busy(monkeypatch):
    """A CPU model takes tens of seconds per round. A terminal that prints
    nothing for that long is indistinguishable from one that has hung."""
    import asyncio

    updates = []

    class FakeStatus:
        def update(self, text):
            updates.append(str(text))

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(cli.console, "status", lambda *a, **k: FakeStatus())

    async def work(on_status):
        await asyncio.sleep(0.5)
        on_status("reading the graph: codeorbit_explore")
        await asyncio.sleep(0.7)
        return "answer"

    result = asyncio.run(cli._with_progress(work))

    assert result == "answer"
    assert len(updates) >= 2, f"the ticker did not tick: {updates}"
    # It reports the phase it is in, not a generic spinner.
    assert any("reading the graph" in u for u in updates), updates
    assert any("thinking" in u for u in updates), updates
    # And an elapsed counter, which is what separates "slow" from "broken".
    assert any(re.search(r"\d+s", u) for u in updates), updates


def test_the_ticker_is_cancelled_when_the_question_finishes(monkeypatch):
    """A leaked ticker would keep drawing over the answer."""
    import asyncio

    class FakeStatus:
        def update(self, text):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(cli.console, "status", lambda *a, **k: FakeStatus())

    async def main():
        async def work(on_status):
            await asyncio.sleep(0.1)
            return 1

        before = len(asyncio.all_tasks())
        await cli._with_progress(work)
        await asyncio.sleep(0.1)
        return before, len(asyncio.all_tasks())

    before, after = asyncio.run(main())
    assert after <= before, "the progress ticker outlived the question"


def test_the_ticker_does_not_swallow_an_error(monkeypatch):
    import asyncio

    class FakeStatus:
        def update(self, text):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(cli.console, "status", lambda *a, **k: FakeStatus())

    async def boom(on_status):
        raise ValueError("from the model")

    with pytest.raises(ValueError, match="from the model"):
        asyncio.run(cli._with_progress(boom))


def test_ask_once_reports_each_phase():
    """The status text has to come from the loop; the CLI cannot know whether
    it is waiting on the model or on a tool."""
    import asyncio

    seen = []

    class FakeSession:
        async def call_tool(self, name, args):
            class R:
                content = []
            return R()

    replies = [
        {"tool_calls": [{"function": {"name": "codeorbit_explore", "arguments": {}}}]},
        {"content": "done", "_truncated": False},
    ]

    def fake_chat(*a, **k):
        return replies.pop(0)

    import codeorbit.agent as agentmod
    real = agentmod._chat
    agentmod._chat = fake_chat
    try:
        asyncio.run(agentmod.ask_once(
            FakeSession(), [], "q", "m", on_status=seen.append))
    finally:
        agentmod._chat = real

    assert "thinking" in seen[0]
    assert any("codeorbit_explore" in s for s in seen), seen
