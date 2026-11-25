"""Context budgeting: the tools have to cost less than reading the files.

That is the claim the MCP server makes to an agent, and before these it was not
checked anywhere. The tests below pin the three behaviours that make it true -
tapering, within-response overlap, across-session repeats - and one that makes
it safe: a symbol the agent says it has lost comes back in full.
"""
from __future__ import annotations


from pathlib import Path

import pytest

from codeorbit import budget, context, db, mcp_server, query
from codeorbit.indexer import index_project
from codeorbit.resolve import resolve_project


@pytest.fixture
def project(tmp_path: Path):
    # Long bodies on purpose: budgets, overlap and elision are all invisible on
    # a two-line function, and a fixture that cannot show the behaviour would
    # let the tests pass while the behaviour was gone.
    run_body = "\n".join(f"        step_{i}()" for i in range(60))
    helper_body = "\n".join(f"    detail_{i}()" for i in range(40))
    (tmp_path / "core.py").write_text(
        "class Engine:\n"
        '    """A long class so budgets and overlap are observable."""\n'
        "\n"
        "    def run(self):\n"
        f"{run_body}\n"
        "        return helper()\n"
        "\n"
        "\n"
        "def helper():\n"
        f"{helper_body}\n"
        "    return 1\n",
        encoding="utf-8")
    index_project(tmp_path)
    resolve_project(tmp_path)
    conn = db.connect(tmp_path)
    yield tmp_path, conn
    conn.close()


# ------------------------------------------------------------- allocation

def test_the_best_match_keeps_its_full_body():
    """Tapering must not shorten the symbol most likely to hold the answer."""
    assert budget.allocate(4, 120)[0] == 120


def test_later_picks_cost_less_than_the_first():
    lines = budget.allocate(4, 120)
    assert lines == sorted(lines, reverse=True)
    assert sum(lines) < 4 * 120


def test_the_tail_stays_long_enough_to_recognise():
    """A budget of zero would return a header with no code, which is worse
    than not returning the symbol at all."""
    assert min(budget.allocate(8, 120, floor=18)) >= 18


def test_allocating_for_nothing_is_empty_not_an_error():
    assert budget.allocate(0, 120) == []


# ------------------------------------------------------------------ ledger

def test_a_span_inside_one_already_printed_is_not_reprinted():
    """Retrieval routinely returns a class and one of its own methods."""
    led = budget.Ledger()
    led.start_response()
    assert led.claim("a.py", 1, 100) is True
    assert led.claim("a.py", 10, 20) is False, "the method is inside the class"
    assert led.claim("b.py", 10, 20) is True, "different file"


def test_a_new_response_forgets_what_the_last_one_printed():
    led = budget.Ledger()
    led.start_response()
    led.claim("a.py", 1, 100)
    led.start_response()
    assert led.claim("a.py", 1, 100) is True


def test_a_body_is_sent_in_full_before_it_is_elided():
    """One send is not enough. An agent's context is compacted without
    warning, and a symbol asked about twice running is one it is working on."""
    led = budget.Ledger(full_sends=2)
    assert led.should_send_body(7)
    led.record(7)
    assert led.should_send_body(7)
    led.record(7)
    assert not led.should_send_body(7)


def test_forgetting_a_symbol_makes_it_send_again():
    led = budget.Ledger(full_sends=1)
    led.record(7)
    assert not led.should_send_body(7)
    led.forget(7)
    assert led.should_send_body(7)


def test_a_stale_send_is_sent_again():
    """Past the staleness window, assume the agent no longer has it."""
    led = budget.Ledger(full_sends=1, stale_after=0)
    led.record(7)
    assert led.should_send_body(7)


# -------------------------------------------------------------------- trim

def test_runs_of_blank_lines_collapse():
    assert budget.trim_source("a\n\n\n\nb") == "a\n\nb"


def test_trailing_whitespace_goes():
    assert budget.trim_source("a   \nb\t\t") == "a\nb"


def test_code_is_not_otherwise_altered():
    src = "def f():\n    return 1\n"
    assert budget.trim_source(src).strip() == src.strip()


# ------------------------------------------------- the server, end to end

def test_the_same_picks_cost_less_than_the_flat_format(project):
    """The same symbols, formatted the old way and the new way. Tapering and
    overlap elision together should take a real bite out of the total."""
    root, conn = project
    picks = context.retrieve(conn, "Engine run helper", max_symbols=4)
    assert len(picks) > 1, "the comparison needs more than one pick"

    flat = "\n\n".join(
        mcp_server._fmt_symbol(root, conn, r, body_lines=120, ledger=None)
        for r in picks)

    led = budget.Ledger()
    led.start_response()
    tuned = "\n\n".join(
        mcp_server._fmt_symbol(root, conn, r, body_lines=lines, ledger=led)
        for r, lines in zip(picks, budget.allocate(len(picks), 120)))

    assert budget.estimate_tokens(tuned) < budget.estimate_tokens(flat)


def test_a_class_and_its_own_method_are_not_both_printed(project):
    """Their bodies overlap exactly, and retrieval returns both routinely."""
    root, conn = project
    cls = query.search(conn, "Engine")[0]
    method = next(r for r in query.search(conn, "run") if r["kind"] == "method")

    led = budget.Ledger()
    led.start_response()
    mcp_server._fmt_symbol(root, conn, cls, body_lines=200, ledger=led)
    second = mcp_server._fmt_symbol(root, conn, method, body_lines=200, ledger=led)
    assert "already shown above in this response" in second


def test_a_repeated_question_stops_resending_the_same_bodies(project):
    root, _ = project
    mcp_server.LEDGER = budget.Ledger(full_sends=1)
    first = mcp_server.codeorbit_explore("how does Engine run",
                                         project_path=str(root))
    again = mcp_server.codeorbit_explore("how does Engine run",
                                         project_path=str(root))
    assert budget.estimate_tokens(again) < budget.estimate_tokens(first)
    # explore now moves past what the session was shown, so a repeat either
    # brings new candidates or says everything matching was already shown -
    # it never sends the first answer's bodies again.
    first_names = {l for l in first.splitlines() if l.startswith("## ")}
    assert first_names.isdisjoint(l for l in again.splitlines() if l.startswith("## "))
    assert "already" in again.lower()


def test_detail_full_brings_an_elided_body_back(project):
    """The escape hatch. Without it, an agent whose context was compacted is
    stuck with a citation it cannot follow."""
    root, _ = project
    mcp_server.LEDGER = budget.Ledger(full_sends=1)
    mcp_server.codeorbit_node("helper", project_path=str(root))
    elided = mcp_server.codeorbit_node("helper", project_path=str(root))
    assert "already returned" in elided

    full = mcp_server.codeorbit_node("helper", detail="full",
                                     project_path=str(root))
    assert "```" in full and "return 1" in full


def test_signatures_mode_returns_structure_without_bodies(project):
    root, _ = project
    mcp_server.LEDGER = budget.Ledger()
    out = mcp_server.codeorbit_explore("Engine", detail="signatures",
                                       project_path=str(root))
    assert "```" not in out
    assert "Engine" in out


def test_a_truncated_body_says_how_to_get_the_rest(project):
    """Silently cutting a body off mid-function teaches an agent that the
    tool is unreliable and to read the file instead."""
    root, conn = project
    row = query.search(conn, "run")[0]
    out = mcp_server._fmt_symbol(root, conn, row, body_lines=5,
                                 ledger=budget.Ledger())
    assert "body continues to line" in out
