"""codeorbit_explore's compact shape: what gets source, what is listed, and
which lines of a long body are kept."""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from codeorbit import budget, db, mcp_server as ms
from codeorbit.indexer import index_project
from codeorbit.resolve import resolve_project


@pytest.fixture(scope="module")
def project(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("explore")
    (root / "pkg").mkdir()
    long_body = "\n".join(f"    x{i} = {i}" for i in range(150))
    (root / "pkg" / "proxy.py").write_text(textwrap.dedent('''
        class ProxyManager:
            """Keeps proxies."""
            def rebuild_proxy_auth(self, request):
                return strip_proxy_header(request)

        def strip_proxy_header(request):
            def inner_proxy_header():
                return request
            return inner_proxy_header()
    ''') + "\n\ndef send_request(request):\n" + long_body
        + "\n    proxy_header = request.proxy_header\n    return proxy_header\n",
        encoding="utf-8")
    (root / "tests").mkdir()
    (root / "tests" / "test_proxy.py").write_text(
        "def test_proxy_header_is_stripped_from_proxy_auth():\n"
        "    proxy_header = 'proxy auth header'\n    assert proxy_header\n",
        encoding="utf-8")
    index_project(root)
    resolve_project(root)
    return root


def explore(root: Path, q: str) -> str:
    ms.LEDGER = budget.Ledger()
    conn = db.connect(root)
    try:
        return ms.explore_text(root, conn, q, max_symbols=2)
    finally:
        conn.close()


def headers(text: str) -> list[str]:
    return [l[3:] for l in text.splitlines() if l.startswith("## ")]


def test_test_code_comes_after_the_code_it_tests(project):
    conn = db.connect(project)
    rows = ms.explore_rows(project, conn, "proxy header stripped from proxy auth", 10)
    conn.close()
    paths = [r["path"] for r in rows]
    tests = [i for i, p in enumerate(paths) if p.startswith("tests/")]
    code = [i for i, p in enumerate(paths) if not p.startswith("tests/")]
    assert tests and code and max(code) < min(tests)


def test_a_question_about_tests_keeps_them_in_place(project):
    conn = db.connect(project)
    rows = ms.explore_rows(project, conn, "which test checks the proxy header", 10)
    conn.close()
    assert rows[0]["path"].startswith("tests/")


def test_source_goes_to_functions_not_classes_or_nested_ones(project):
    out = explore(project, "ProxyManager rebuild_proxy_auth strip_proxy_header")
    got = headers(out)
    assert all(h.split()[0] in ("function", "method") for h in got)
    assert not any("inner_proxy_header" in h for h in got)     # inside a pick
    assert "class pkg.proxy.ProxyManager" in out                # listed instead
    assert "members: rebuild_proxy_auth" in out


def test_a_long_body_keeps_the_lines_the_question_is_about():
    lines = [f"{i:>4} | x{i} = {i}" for i in range(1, 201)]
    lines[149] = " 150 | proxy_header = request.proxy_header"
    kept = ms._focus(lines, 30, {"proxy", "header"})
    assert len([l for l in kept if "|" in l]) <= 30
    assert kept[0].startswith("   1 |")                         # signature first
    assert any("proxy_header" in l for l in kept)
    assert "   ..." in kept


def test_a_short_body_is_untouched():
    lines = [f"{i} | y" for i in range(10)]
    assert ms._focus(lines, 30, {"y"}) == lines


def test_source_is_capped_and_lands_on_the_matching_lines(project):
    # Not "always smaller than classic": on a small project classic can return
    # four tiny wrong functions. What compact guarantees is a cap on source
    # lines, spent on the lines the question names.
    out = explore(project, "send_request proxy header")
    assert "pkg.proxy.send_request" in out
    source = [l for l in out.splitlines() if " | " in l]
    assert len(source) <= sum(budget.allocate(2, ms.EXPLORE_BODY_LINES))
    assert any("proxy_header = request.proxy_header" in l for l in source)


def _session_calls(root: Path, q: str, n: int) -> list[str]:
    ms.LEDGER = budget.Ledger()                 # one agent session
    conn = db.connect(root)
    try:
        return [ms.explore_text(root, conn, q, max_symbols=1) for _ in range(n)]
    finally:
        conn.close()


def test_asking_again_brings_new_candidates_not_the_same_ones(project):
    first, second = _session_calls(project, "proxy header", 2)
    assert headers(first) and headers(second)
    assert set(headers(first)).isdisjoint(headers(second))


def test_from_the_second_call_it_names_what_was_already_shown(project):
    # Either form leads with the names already sent.
    calls = _session_calls(project, "proxy header", 2)
    assert "Already shown in this session" not in calls[0]
    shown_first = headers(calls[0])[0].split()[-1]
    first_line = calls[1].split("\n", 1)[0]
    assert first_line.startswith(("Already shown in this session",
                                  "Everything that matches this was already shown"))
    assert shown_first in first_line


def test_when_everything_was_shown_it_says_so(project):
    calls = _session_calls(project, "proxy header", 12)
    assert calls[-1].startswith("Everything that matches this was already shown")


def test_full_detail_resends_what_was_shown(project):
    ms.LEDGER = budget.Ledger()
    conn = db.connect(project)
    try:
        a = ms.explore_text(project, conn, "proxy header", max_symbols=1)
        b = ms.explore_text(project, conn, "proxy header", max_symbols=1, detail="full")
    finally:
        conn.close()
    assert headers(a) == headers(b)


def test_explore_ends_with_the_best_match_to_answer_with(project):
    out = _session_calls(project, "proxy header", 1)[0]
    last = out.strip().splitlines()[-1]
    assert last.startswith("Best match: ") and ".py:" in last
