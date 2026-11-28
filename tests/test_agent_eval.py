import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import agent_eval as ae  # noqa: E402

T = "requests.sessions.Session.request"
F = "requests/sessions.py"


def test_parse_location_variants():
    assert ae.parse_location("requests/sessions.py:Session.request") == (F, "Session.request")
    assert ae.parse_location("./requests/sessions.py::Session.request()") == (F, "Session.request")
    assert ae.parse_location("requests/sessions.py:120") == (F, None)
    assert ae.parse_location("Session.request") == (None, "Session.request")
    assert ae.parse_location("`requests\\sessions.py`") == (F, None)


def test_fn_match_needs_name_tail_and_right_file():
    assert ae.fn_match("requests/sessions.py:Session.request", T, F)
    assert ae.fn_match("sessions.py:request", T, F)
    assert ae.fn_match("requests.sessions.Session.request", T, F)
    assert ae.fn_match("Session.request", T, F)                     # no path given
    assert not ae.fn_match("requests/api.py:request", T, F)         # wrong file
    assert not ae.fn_match("requests/sessions.py:quest", T, F)      # not a whole name
    assert not ae.fn_match("requests/sessions.py", T, F)            # a file is not a function


def test_score_answer_ranks():
    s = ae.score_answer(["requests/api.py:get", "requests/sessions.py:Session.request"],
                        [T], {T: F}, [F])
    assert s["fn_rank"] == 2 and s["fn_hit"] and not s["fn_hit1"] and s["fn_all"]
    assert s["file_rank"] == 2
    empty = ae.score_answer(None, [T], {T: F}, [F])
    assert not empty["fn_hit"] and not empty["answered"]


def test_inline_tool_calls():
    calls = ae.inline_calls('I will search. {"name": "grep", "arguments": {"pattern": "foo"}}')
    assert calls == [{"function": {"name": "grep", "arguments": {"pattern": "foo"}}}]
    assert ae.inline_calls("no json here") == []


def test_mcnemar_exact():
    assert ae.mcnemar(0, 0) == 1.0
    assert abs(ae.mcnemar(10, 0) - 2 / 1024) < 1e-12
    assert ae.mcnemar(5, 5) == 1.0


def test_compare_pairs():
    def row(cond, hit, tok):
        return {"question_id": "q", "condition": cond, "kind": "named", "targets": [T],
                "fn_hit": hit, "fn_hit1": hit, "fn_all": hit, "file_hit": hit,
                "file_hit1": hit, "answered": True, "input_tokens": tok,
                "output_tokens": 10, "tool_tokens": 5, "turns": 3, "seconds": 1.0,
                "tool_calls": {"grep": 1}}
    c = ae.compare(ae.paired([row("without", False, 900), row("with", True, 600)]))
    assert c["fn_hit"]["only_with"] == 1 and c["fn_hit"]["with"] == 1.0
    assert c["input_tokens"]["with_lower"] == 1


def test_workspace_tools(tmp_path):
    import subprocess
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "a.py").write_text("def foo():\n    return 1\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    if subprocess.run(["git", "add", "."], cwd=tmp_path).returncode:
        import pytest
        pytest.skip("git cannot write in the temporary directory here")
    ws = ae.Workspace(tmp_path)
    assert "pkg/a.py:1:def foo():" in ws.grep("def foo")
    assert "No matches" in ws.grep("bar(")                   # invalid regex -> literal
    assert "    2     return 1" in ws.read_file("pkg/a.py")
    assert ws.list_dir() == "pkg/"
    assert "outside" in ae.call_tool("read_file", {"path": "../x"}, ws, None)


def test_answer_list_unpacks_json_strings():
    assert ae.answer_list(['["a.py:f", "b.py:g"]']) == ["a.py:f", "b.py:g"]
    assert ae.answer_list('["a.py:f"]') == ["a.py:f"]
    assert ae.answer_list("a.py:f, b.py:g") == ["a.py:f", "b.py:g"]
    assert ae.answer_list(None) == []


def test_parse_location_ignores_what_follows_the_name():
    assert ae.parse_location("requests/sessions.py:resolve_redirects:r = r.clone()") == \
        ("requests/sessions.py", "resolve_redirects")
    assert ae.parse_location("src/lib.rs:Parser::parse") == ("src/lib.rs", "Parser.parse")
    assert ae.parse_location("lib/a.rb#Foo#bar") == ("lib/a.rb", "Foo.bar")
    assert ae.parse_location("pkg/x.go:42") == ("pkg/x.go", None)


def test_locations_written_in_prose_are_read():
    from agent_eval import text_locations
    text = ("The bug is in `src/_pytest/unittest.py:TestCaseFunction.runtest`, and also "
            "maybe src/_pytest/unittest.py:_is_skipped. See src/_pytest/unittest.py too.")
    assert text_locations(text) == ["src/_pytest/unittest.py:TestCaseFunction.runtest",
                                    "src/_pytest/unittest.py:_is_skipped"]
    assert text_locations("no locations here") == []


def test_inject_profile_puts_codeorbit_in_the_issue_not_in_the_tools(tmp_path, monkeypatch):
    from codeorbit.indexer import index_project
    from codeorbit.resolve import resolve_project
    (tmp_path / "proxy.py").write_text(
        "def rebuild_proxy_auth(request):\n    request.headers.pop('Proxy-Authorization')\n",
        encoding="utf-8")
    index_project(tmp_path)
    resolve_project(tmp_path)
    seen = {}

    def fake_chat(model, messages, tools, seed, url):
        seen["user"], seen["tools"] = messages[1]["content"], [t["function"]["name"] for t in tools]
        return {"message": {"role": "assistant", "content": "", "tool_calls": [
            {"function": {"name": "finish",
                          "arguments": {"locations": ["proxy.py:rebuild_proxy_auth"]}}}]}}

    monkeypatch.setattr(ae, "chat", fake_chat)
    monkeypatch.setattr(ae, "PROFILE", "inject")
    rec = ae.episode("m", "with", "Proxy-Authorization kept on redirect", ae.Workspace(tmp_path),
                     len, 0, "", None)
    assert rec["answer"] == ["proxy.py:rebuild_proxy_auth"]
    assert "CodeOrbit (automatic" in seen["user"] and "rebuild_proxy_auth" in seen["user"]
    assert not any(t.startswith("codeorbit") for t in seen["tools"])


def test_a_turn_ollama_cannot_answer_ends_the_episode_not_the_run(tmp_path, monkeypatch):
    calls = []

    class Resp:
        status_code, text = 500, "error parsing tool call"

    def fake_post(url, json, timeout):
        calls.append(json["options"]["seed"])
        return Resp()

    monkeypatch.setattr(ae.requests, "post", fake_post)
    monkeypatch.setattr(ae, "PROFILE", "full")
    rec = ae.episode("gpt-oss:20b", "without", "an issue", ae.Workspace(tmp_path),
                     len, 7, "http://x", None)
    assert rec["stop"] == "server_error" and rec["answer"] is None
    assert calls == [7, 8, 9, 10]                 # retried with other seeds
