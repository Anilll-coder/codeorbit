"""The Claude Code harness: answer parsing and reading stream-json, without a model."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import claude_eval as ce  # noqa: E402


def test_the_answer_block_is_read_from_the_last_answer_line():
    text = ("I looked at sessions.py.\nANSWER: draft\n\nOn reflection:\n"
            "ANSWER:\n1. requests/sessions.py:Session.rebuild_proxies\n"
            "- `requests/adapters.py:HTTPAdapter.send`\n")
    assert ce.parse_answer(text) == ["requests/sessions.py:Session.rebuild_proxies",
                                     "requests/adapters.py:HTTPAdapter.send"]


def test_answer_on_the_same_line_and_bold_marker():
    assert ce.parse_answer("**ANSWER:** a.py:f\nb.py:C.m") == ["a.py:f", "b.py:C.m"]


def test_no_answer_line_is_no_answer():
    assert ce.parse_answer("I could not find it.") is None


def test_at_most_five_locations():
    text = "ANSWER:\n" + "\n".join(f"a.py:f{i}" for i in range(9))
    assert len(ce.parse_answer(text)) == 5


def _ev(**kw):
    return json.dumps(kw)


def test_stream_gives_tokens_tools_and_answer():
    out = "\n".join([
        _ev(type="system", subtype="init"),
        _ev(type="assistant", message={"usage": {"input_tokens": 10, "cache_read_input_tokens": 900,
                                                 "cache_creation_input_tokens": 100},
                                       "content": [{"type": "tool_use", "name": "Grep"},
                                                   {"type": "tool_use",
                                                    "name": "mcp__codeorbit__codeorbit_explore"}]}),
        _ev(type="user", message={"content": [{"type": "tool_result", "content": "x" * 40},
                                              {"type": "tool_result",
                                               "content": [{"type": "text", "text": "y" * 40}]}]}),
        _ev(type="assistant", message={"usage": {"input_tokens": 5}, "content": [
            {"type": "text", "text": "ANSWER:\nm.py:f"}]}),
        _ev(type="result", subtype="success", is_error=False, num_turns=2,
            result="ANSWER:\nm.py:f", total_cost_usd=0.01,
            usage={"input_tokens": 15, "cache_read_input_tokens": 900,
                   "cache_creation_input_tokens": 100, "output_tokens": 42},
            modelUsage={"claude-sonnet-x": {}}),
    ])
    rec = ce.parse_stream(out, 3.0, lambda s: len(s) // 4)
    assert rec["answer"] == ["m.py:f"] and rec["answered"]
    assert rec["input_tokens"] == 1015 and rec["output_tokens"] == 42
    assert rec["tool_calls"] == {"Grep": 1, "codeorbit_explore": 1}
    assert rec["tool_tokens"] == 20 and rec["peak_context"] == 1010
    assert rec["turns"] == 2 and rec["models"] == ["claude-sonnet-x"]


def test_an_error_with_no_text_is_an_error():
    out = _ev(type="result", is_error=True, result="Claude AI usage limit reached", num_turns=0)
    rec = ce.parse_stream(out, 1.0, len)
    assert "error" in rec and ce.LIMIT_WORDS.search(rec["error"])


def test_no_result_event_is_an_error():
    assert "error" in ce.parse_stream("", 1.0, len)


def test_the_index_is_hidden_from_text_search(tmp_path):
    (tmp_path / ".git" / "info").mkdir(parents=True)
    ce.hide_index(tmp_path)
    ce.hide_index(tmp_path)
    text = (tmp_path / ".git" / "info" / "exclude").read_text()
    assert text.count(".codeorbit/") == 1


def test_codeorbit_tools_match_the_open_model_harness():
    assert [t.split("__")[-1] for t in ce.CO_TOOLS] == list(ce.ae.CO_PARAMS)


def test_opencode_events_give_steps_tokens_tools_and_answer():
    ev = lambda t, **p: json.dumps({"type": t, "part": p})
    out = "\n".join([
        ev("step_start"),
        ev("tool_use", tool="grep", state={"status": "completed", "output": "x" * 40}),
        ev("tool_use", tool="codeorbit_codeorbit_explore", state={"output": "y" * 80}),
        ev("step_finish", reason="tool-calls", tokens={"input": 4909, "output": 84, "reasoning": 0,
                                                       "cache": {"write": 0, "read": 0}}),
        ev("text", text="It is rebuild_proxies.\n\nANSWER: requests/sessions.py:SessionRedirectMixin.rebuild_proxies"),
        ev("step_finish", reason="stop", tokens={"input": 2625, "output": 43, "reasoning": 63,
                                                 "cache": {"write": 0, "read": 4608}}),
    ])
    rec = ce.parse_opencode(out, 9.0, lambda s: len(s) // 4)
    assert rec["answer"] == ["requests/sessions.py:SessionRedirectMixin.rebuild_proxies"]
    assert rec["turns"] == 2 and rec["stop"] == "finish"
    assert rec["input_tokens"] == 4909 + 2625 + 4608 and rec["output_tokens"] == 84 + 43 + 63
    assert rec["tool_calls"] == {"grep": 1, "codeorbit_explore": 1}
    assert rec["tool_tokens"] == 30 and rec["peak_context"] == 7233


def test_opencode_error_with_no_steps_is_an_error():
    out = json.dumps({"type": "error", "error": {"name": "APIError",
                                                 "data": {"message": "Model is unavailable."}}})
    assert "unavailable" in ce.parse_opencode(out, 1.0, len)["error"]


def test_opencode_config_denies_everything_but_reading(tmp_path):
    without = ce.opencode_config("without", tmp_path, 20)
    perm = without["agent"]["build"]["permission"]
    assert without["agent"]["build"]["steps"] == 20 and "mcp" not in without
    assert all(perm[k] == "allow" for k in ce.OC_BASE)
    assert all(perm[k] == "deny" for k in ("edit", "bash", "webfetch", "task"))
    with_ = ce.opencode_config("with", tmp_path, 20)
    assert with_["mcp"]["codeorbit"]["command"][-3:] == ["mcp", "--path", str(tmp_path)]
    assert with_["agent"]["build"]["permission"]["codeorbit_codeorbit_audit"] == "deny"
