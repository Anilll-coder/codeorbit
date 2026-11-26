"""MCP server: expose the code graph to any AI agent that speaks MCP.

The central design decision is that these tools return CONTEXT, not answers.

`codeorbit ask` exists because a developer at a terminal has no model to hand,
so a local 3.8B one answers. An agent connecting over MCP already has a far
stronger model - it does not need phi4-mini's answer, it needs the surgical
context that would let its own model answer well. So `explore` returns the
subgraph: the symbols a question is about, their real source, who calls them and
what they call. The agent reasons; the graph retrieves.

Three rules shape the output, and they are about how agents actually behave:

  1. Be sufficient. An agent falls back to reading files the moment a tool's
     answer is not enough, and every fallback costs a turn. So a symbol comes
     back with its full body AND its call neighbourhood in one response.
  2. Never send the agent to read a file. If more is needed, point at another
     tool here.
  3. An expected condition is not an error. "Not indexed" and "no such symbol"
     return ordinary results carrying guidance, because a tool that raises
     errors trains an agent to stop calling it - after which none of this is
     used at all.

Nothing may be written to stdout except protocol traffic: stdout IS the
transport. Every diagnostic goes to stderr.
"""
from __future__ import annotations

import sys
from pathlib import Path

from mcp.server import MCPServer

from . import audit as auditmod
from . import budget as budgetmod
from . import context as ctxmod
from . import db, query
from .db import DB_DIRNAME

# Set from the CLI; any tool may override it per-call with project_path.
DEFAULT_ROOT: Path = Path.cwd()

MAX_BODY_LINES = 120
FULL_BODY_LINES = 600       # what detail="full" is allowed to spend
MAX_CHARS = 24_000          # keep one response inside a sane slice of context

# What has already gone to this agent. One per server process, which is one per
# agent session, so it measures exactly the right thing: what is in the context
# window on the other end of the pipe.
LEDGER = budgetmod.Ledger()

INSTRUCTIONS = """\
CodeOrbit: a call graph of this repository with the real source behind it.
Start with codeorbit_explore: it returns the best-matching functions with their
source, callers and callees, and lists further candidates one line each. Treat
returned source as read - do not re-open those files; check a listed candidate
with codeorbit_node instead. When a response already shows what you need, stop
searching and answer. A body sent earlier comes back as a citation;
detail="full" resends it. [uncertain] edges were resolved by name only.
"""


def log(msg: str) -> None:
    print(f"[codeorbit-mcp] {msg}", file=sys.stderr, flush=True)


def _resolve_root(project_path: str | None) -> Path:
    return Path(project_path).resolve() if project_path else DEFAULT_ROOT


def _not_indexed(root: Path) -> str:
    return (
        f"No CodeOrbit index exists for {root}.\n\n"
        f"The graph is built once, on disk, under {DB_DIRNAME}/. Ask the user to "
        f"run:\n\n    codeorbit index {root}\n\n"
        "Indexing is deliberately the user's decision, not something this server "
        "does on its own."
    )


def _fmt_symbol(root: Path, conn, row, body_lines: int = MAX_BODY_LINES,
                ledger: budgetmod.Ledger | None = None, source: bool = True,
                neighbours: tuple[int, int] = (10, 12),
                focus: set[str] | None = None) -> str:
    """One symbol: what it is, what touches it, and its body.

    `body_lines` is a budget rather than a constant, and `ledger` is what keeps
    the same body from being billed to the agent twice. Both exist because the
    tool's whole claim is that calling it costs less than reading the file, and
    a flat 120 lines per symbol times four symbols per call is not obviously
    cheaper than just opening one file.
    """
    body = _body(root, row, body_lines, ledger, focus) if source else ""
    out = [
        f"## {row['kind']} {row['qname']}",
        f"{row['path']}:{row['start_line']}-{row['end_line']}  ({row['lang']})",
    ]
    # Not repeated when the body below already carries them (the def line, a
    # Python docstring). An elided body is a citation, so they stay then.
    if row["signature"] and row["signature"].strip() not in body:
        out.append(f"signature: {row['signature']}")
    if row["docstring"] and row["docstring"].strip()[:60] not in body:
        out.append(f"doc: {row['docstring'][:400]}")

    # One line per neighbour with all its call lines, not one per call site.
    callers = ctxmod.group_neighbours(query.callers(conn, row["id"], limit=40), neighbours[0])
    if callers:
        out.append("\ncalled by:")
        for c in callers:
            flag = "" if c["resolution"] == "exact" else "  [uncertain]"
            out.append(f"  {c['qname']}  ({c['path']}:{ctxmod.call_lines(c)}){flag}")
    else:
        out.append("\ncalled by: nothing in this project")

    callees = ctxmod.group_neighbours(query.callees(conn, row["id"], limit=48), neighbours[1])
    if callees:
        out.append("\ncalls:")
        for c in callees:
            flag = "" if c["resolution"] == "exact" else "  [uncertain]"
            out.append(f"  {c['qname']}  (line {ctxmod.call_lines(c)}){flag}")

    if row["kind"] == "class":
        members = query.members(conn, row["id"])
        if members:
            out.append("\nmembers: " + ", ".join(m["name"] for m in members[:25]))

    if body:
        out.append(body)
    return "\n".join(out)


def _focus(lines: list[str], budget: int, terms: set[str],
           head: int = 3, radius: int = 5) -> list[str]:
    """At most `budget` of a long body's lines: its first `head` (the
    signature), then the stretches around the lines that share the most words
    with the question, in source order, with the gaps marked.

    The first N lines of a 300-line function are rarely where an issue lives;
    the lines that mention what the issue mentions usually are.
    """
    from .rank import code_tokens
    n = len(lines)
    if n <= budget:
        return lines
    score = [sum(t in terms for t in code_tokens(l.split("|", 1)[-1])) for l in lines]
    keep = set(range(min(head, n)))
    for i in sorted(range(n), key=lambda i: (-score[i], i)):
        if score[i] == 0 or len(keep) >= budget:
            break
        for j in range(max(0, i - radius), min(n, i + radius + 1)):
            if len(keep) >= budget:
                break
            keep.add(j)
    for j in range(n):                      # nothing matched well: plain top
        if len(keep) >= budget:
            break
        keep.add(j)
    out, prev = [], -1
    for i in sorted(keep):
        if i != prev + 1:
            out.append("   ...")
        out.append(lines[i])
        prev = i
    if prev < n - 1:
        out.append("   ...")
    return out


def _body(root: Path, row, body_lines: int, ledger: budgetmod.Ledger | None,
          focus: set[str] | None = None) -> str:
    """The source, a pointer to it, or nothing. With `focus`, a body longer
    than the budget keeps the lines that match those words, not its top."""
    start, end = row["start_line"], row["end_line"]
    cite = f"{row['path']}:{start}-{end}"

    if ledger is not None:
        if not ledger.claim(row["path"], start, min(end, start + body_lines)):
            return ("\nsource: already shown above in this response "
                    f"({cite}) - it is inside a symbol already printed.")
        if not ledger.should_send_body(row["id"]):
            return (f"\nsource: already returned to you earlier in this session "
                    f"({cite}), unchanged since. If it is no longer in your "
                    f"context, call codeorbit_node with detail=\"full\".")

    truncated = (end - start + 1) > body_lines
    if focus and truncated:
        whole = query.source_of(root, row, max_lines=end - start + 1)
        src = "\n".join(_focus(whole.split("\n"), body_lines, focus)) if whole else ""
    else:
        src = query.source_of(root, row, max_lines=body_lines)
    if not src:
        return ""
    if ledger is not None:
        ledger.record(row["id"])

    tail = ((f"\n[{body_lines} of {end - start + 1} lines, chosen by the question; "
             if focus else f"\n[body continues to line {end}; ")
            + "codeorbit_node with detail=\"full\" returns all of it]"
            if truncated else "")
    return f"\n```{row['lang']}\n{budgetmod.trim_source(src)}\n```{tail}"


def _ledger_for(detail: str, node_ids: list[int]) -> budgetmod.Ledger:
    """The session ledger, reset for the symbols this call must resend.

    `detail="full"` is the agent saying "I no longer have that", and the only
    correct response is to forget having sent it and send it again.
    """
    if detail == "full":
        for nid in node_ids:
            LEDGER.forget(nid)
    LEDGER.start_response()
    return LEDGER


def _clip(text: str) -> str:
    if len(text) <= MAX_CHARS:
        return text
    return (text[:MAX_CHARS] + "\n\n[truncated. Narrow the query, or call "
            "codeorbit_node on one symbol for its full body.]")


def _with_graph(project_path: str | None):
    """Yield (root, conn) or (root, None) when the project has no index."""
    root = _resolve_root(project_path)
    try:
        return root, db.connect(root)
    except FileNotFoundError:
        return root, None


def graph_tool(fn):
    """Open the graph, hand it to `fn`, and never let an exception escape.

    Every tool needs the same three things - resolve the project, open its
    graph, close it afterwards - and must not raise, because the SDK turns an
    escaping exception into a protocol-level tool error. That matters more than
    it sounds: one or two errors early in a session and an agent stops calling
    the tool at all, after which the graph may as well not exist.

    This was learned the hard way. A transient "database disk image is
    malformed" - the graph being read while a concurrent `codeorbit index` was
    still checkpointing its WAL - propagated as UnexpectedToolError, and all the
    agent received was "Error executing tool codeorbit_explore". Every failure
    now comes back as ordinary text saying what to do next.
    """
    import functools
    import inspect

    @functools.wraps(fn)
    def wrapper(*args, project_path: str | None = None, **kwargs) -> str:
        root, conn = _with_graph(project_path)
        if conn is None:
            return _not_indexed(root)
        try:
            return fn(root, conn, *args, **kwargs)
        except Exception as e:  # noqa: BLE001 - a tool must never raise
            log(f"{fn.__name__} failed: {e!r}")
            return (
                f"Could not complete {fn.__name__}: {e}\n\n"
                "This is usually a stale or half-written index - it can happen "
                "if the graph was read while `codeorbit index` was still "
                f"writing. Ask the user to run:\n\n    codeorbit index {root}\n\n"
                "then try again. Other CodeOrbit tools may still work."
            )
        finally:
            try:
                conn.close()
            except Exception:
                pass

    # MCPServer builds each tool's JSON schema from the signature, and
    # functools.wraps leaves __wrapped__ behind, which inspect.signature follows
    # straight back to (root, conn, ...) - so the client was asked to supply a
    # database handle. Publish the signature clients actually see: the tool's
    # own parameters, minus the injected two, plus project_path.
    params = [p for name, p in inspect.signature(fn).parameters.items()
              if name not in ("root", "conn")]
    params.append(inspect.Parameter(
        "project_path", inspect.Parameter.KEYWORD_ONLY,
        default=None, annotation="str | None"))
    wrapper.__signature__ = inspect.Signature(params, return_annotation=str)
    del wrapper.__wrapped__

    return wrapper


mcp = MCPServer(name="codeorbit", instructions=INSTRUCTIONS)


@mcp.tool()
@graph_tool
def codeorbit_explore(root, conn, query_text: str, max_symbols: int = 2,
                      detail: str = "auto") -> str:
    """Start here. Finds the functions a question or issue is about: their
    source, callers and callees, then more candidates one line each. Naming
    symbols sharpens it. max_symbols: matches sent with source (default 2).
    detail: "auto"; "full" resends; "signatures" sends no bodies.
    """
    return explore_text(root, conn, query_text, max_symbols, detail)


# explore's shape. Measured, not guessed: the classic shape (four bodies of up
# to 120 lines, ten callers and twelve callees each) was most of the ~10k tool
# tokens per task in the agent pilot. More candidates, fewer bodies.
EXPLORE_BODY_LINES = 80     # the best match's source; the next gets about half
EXPLORE_MORE = 6            # further candidates, one line each
EXPLORE_NEIGHBOURS = (5, 6)  # callers, callees per symbol
EXPLORE_REMIND_AFTER = 2    # from this call on, name what was already shown
IMPACT_LINES, IMPACT_TESTS = 25, 10


def explore_text(root: Path, conn, query_text: str, max_symbols: int = 2,
                 detail: str = "auto", style: str = "compact") -> str:
    """explore's response. `style="classic"` is the previous shape and ranking,
    kept so the benchmark can score both on the same tasks."""
    if style == "classic":
        picks, more = ctxmod.retrieve(conn, query_text, max_symbols=max_symbols), []
        budget, near = MAX_BODY_LINES, (10, 12)
    else:
        # A repeat call moves down the ranking: what this session has already
        # been shown is skipped, so a rephrased question brings new candidates
        # instead of the same ones again. detail="full" is a request to resend.
        seen = LEDGER.shown if detail != "full" else {}
        late = LEDGER.explore_calls + 1 >= EXPLORE_REMIND_AFTER and detail != "full"
        if late:
            # Deep into a search: candidates matter more than source now, and
            # every token sent is re-read on every later turn.
            max_symbols = min(max_symbols, 1)
        rows = [r for r in explore_rows(root, conn, query_text,
                                        max_symbols + EXPLORE_MORE + len(seen))
                if r["id"] not in seen][:max_symbols + EXPLORE_MORE]
        # Source goes to functions and methods: a class's "body" is its first
        # lines - attributes, a docstring - while the method that matters is
        # cut off. Classes stay in the list, with their members named.
        # A function nested in one already chosen is inside its source.
        picks = []
        for r in rows:
            if len(picks) >= max_symbols:
                break
            if r["kind"] in ("function", "method") and not any(
                    r["qname"].startswith(p["qname"] + ".") for p in picks):
                picks.append(r)
        picks = picks or rows[:max_symbols]
        more = [r for r in rows if r not in picks][:EXPLORE_MORE]
        budget, near = EXPLORE_BODY_LINES, EXPLORE_NEIGHBOURS
        if late:
            budget, near = EXPLORE_BODY_LINES // 2, (3, 3)
    if not picks:
        if style != "classic" and LEDGER.shown:
            return ("Everything that matches this was already shown in this session: "
                    + ", ".join(list(LEDGER.shown.values())[:EXPLORE_MORE])
                    + ".\nIf one of these is what the task needs, answer now; "
                    "otherwise ask about something different, or call "
                    "codeorbit_node on one name.")
        return (f"Nothing in the graph matched: {query_text!r}\n\n"
                "Try codeorbit_search with a single identifier to see what "
                "exists, or codeorbit_overview for the project's main symbols.")

    earlier = list(LEDGER.shown.values()) if style != "classic" else []
    ledger = _ledger_for(detail, [r["id"] for r in picks])
    budgets = budgetmod.allocate(len(picks), budget)
    focus = None
    if style != "classic":
        from .rank import code_tokens
        focus = {t for t in code_tokens(query_text) if t not in ctxmod.STOP and len(t) > 2}
    header = f"Retrieved {len(picks)} symbol(s) for: {query_text[:200]}\n"
    if style != "classic":
        LEDGER.explore_calls += 1
        for r in picks + more:
            LEDGER.shown.setdefault(r["id"], r["qname"])
        if LEDGER.explore_calls >= EXPLORE_REMIND_AFTER and earlier:
            header = (f"Already shown in this session (best first): "
                      f"{', '.join(earlier[:EXPLORE_MORE])}. If one of these is what "
                      f"the task needs, answer now rather than searching again. "
                      f"New candidates follow.\n\n" + header)
    body = "\n\n".join(
        _fmt_symbol(root, conn, r, body_lines=lines, ledger=ledger,
                    source=detail != "signatures", neighbours=near, focus=focus)
        for r, lines in zip(picks, budgets))
    if more:
        body += "\n\nAlso likely (codeorbit_node <name> for any one):\n" + "\n".join(
            _one_line(conn, r) for r in more)
    if style != "classic":
        # One line a small model can act on, instead of more turns confirming it.
        best = picks[0]
        body += (f"\n\nBest match: {best['path']}:{best['qname']}. If it fits, "
                 "answer now; if not, ask about what is missing.")
    return _clip(header + "\n\n" + body)


def _one_line(conn, row) -> str:
    line = f"  {row['kind']} {row['qname']}  ({row['path']}:{row['start_line']}-{row['end_line']})"
    if row["kind"] == "class":
        names = [m["name"] for m in query.members(conn, row["id"])]
        if names:
            line += "  members: " + ", ".join(names[:8]) + (" ..." if len(names) > 8 else "")
    return line


def explore_rows(root: Path, conn, query_text: str, limit: int) -> list:
    """explore's ranking: fusion, with test code after the code it tests
    unless the question is about tests. An agent asked where a bug lives
    wants the implementation; a test helper that repeats the issue's words
    otherwise takes a slot."""
    import re as _re
    from . import rank
    from .rules import is_test_path
    ids = rank.fusion_ranking(root, conn, query_text, limit=limit + 12)
    rows = [r for r in (query.get_node(conn, i) for i in ids) if r is not None]
    if not _re.search(r"\btests?\b|\btesting\b|pytest|unittest", query_text, _re.I):
        is_test = [is_test_path(r["path"] or "") for r in rows]
        rows = ([r for r, t in zip(rows, is_test) if not t]
                + [r for r, t in zip(rows, is_test) if t])
    return rows[:limit]


@mcp.tool()
@graph_tool
def codeorbit_search(root, conn, term: str, limit: int = 20) -> str:
    """Find symbols by name: kind, qualified name, file:line."""
    rows = query.search(conn, term, limit)
    if not rows:
        return (f"No symbol matching {term!r}. Names match exactly first, "
                "then by full text, then by substring - try a shorter "
                "fragment, or codeorbit_overview.")
    lines = [f"{len(rows)} match(es) for {term!r}:", ""]
    lines += [f"  {r['kind']:<9} {r['qname']}  ({r['path']}:{r['start_line']})"
              for r in rows]
    return "\n".join(lines)


@mcp.tool()
@graph_tool
def codeorbit_node(root, conn, name: str, detail: str = "auto") -> str:
    """One symbol: its source, callers and callees; others sharing the name are
    listed. detail="full" resends a body already sent.
    """
    hits = query.search(conn, name, limit=6)
    if not hits:
        return (f"No symbol named {name!r}. Call codeorbit_search to find "
                "the right name.")
    ledger = _ledger_for(detail, [hits[0]["id"]])
    parts = [_fmt_symbol(root, conn, hits[0],
                         body_lines=FULL_BODY_LINES if detail == "full"
                         else EXPLORE_BODY_LINES,
                         ledger=ledger, source=detail != "signatures")]
    if len(hits) > 1:
        parts.append("\nOther symbols share this name:")
        parts += [f"  {h['kind']} {h['qname']} ({h['path']}:{h['start_line']})"
                  for h in hits[1:5]]
    return _clip("\n".join(parts))


@mcp.tool()
@graph_tool
def codeorbit_outline(root, conn, path: str) -> str:
    """A file's functions and classes with lines and signatures, no code -
    cheaper than reading it. `path` may be any unambiguous tail, e.g. "models.py".
    """
    found, rows = query.outline(conn, path)
    if found is None:
        if rows:
            return f"{path!r} matches several files - give more of the path:\n" + \
                "\n".join(f"  {p}" for p in rows)
        return (f"No indexed file {path!r}. codeorbit_search finds a symbol's file; "
                "codeorbit_overview lists the main ones.")
    if not rows:
        return f"{found}: no functions or classes - only module-level code."
    return _clip(ctxmod.format_outline(found, rows))


@mcp.tool()
@graph_tool
def codeorbit_impact(root, conn, name: str, depth: int = 3) -> str:
    """What transitively calls a symbol, and which tests reach it - for judging
    a change's risk, not for finding where a bug is.
    """
    hits = query.search(conn, name, limit=3)
    if not hits:
        return f"No symbol named {name!r}. Call codeorbit_search first."
    row = hits[0]
    reach = query.impact(conn, row["id"], depth)
    tests = query.affected_tests(conn, row["id"], depth + 1)

    out = [f"Changing {row['qname']} ({row['path']}:{row['start_line']}) "
           f"can affect {len(reach)} symbol(s) within {depth} hops.", ""]
    if reach:
        out += [f"  {'  ' * (d - 1)}[{d}] {r['qname']}  ({r['path']})"
                for d, r in reach[:IMPACT_LINES]]
    else:
        out.append("  Nothing in this project calls it.")
    out.append("")
    if tests:
        out.append(f"Tests that reach it ({len(tests)}):")
        out += [f"  {t['qname']}  ({t['path']})" for t in tests[:IMPACT_TESTS]]
    else:
        out.append("NO test reaches this symbol.")
    return _clip("\n".join(out))


@mcp.tool()
@graph_tool
def codeorbit_path(root, conn, from_symbol: str, to_symbol: str,
               max_depth: int = 8) -> str:
    """The call path from one symbol to another, hop by hop with call-site lines.

    Use for "how does A reach B". A missing path means no STATIC path exists;
    they may still be connected at runtime through dynamic dispatch.
    """
    a = query.search(conn, from_symbol, limit=3)
    b = query.search(conn, to_symbol, limit=3)
    if not a:
        return f"No symbol named {from_symbol!r}. Call codeorbit_search first."
    if not b:
        return f"No symbol named {to_symbol!r}. Call codeorbit_search first."

    hops = query.call_path(conn, a[0]["id"], b[0]["id"], max_depth)
    if not hops:
        return (
            f"No static call path from {a[0]['qname']} to {b[0]['qname']} "
            f"within {max_depth} hops.\n\n"
            "They may still be connected at runtime through dynamic dispatch "
            "- a decorator-registered handler, a dict of callbacks, a "
            "framework hook - which static parsing cannot follow. "
            "codeorbit_impact on either end shows what IS statically "
            "connected."
        )
    out = [f"{len(hops) - 1} hop(s): {a[0]['qname']} -> {b[0]['qname']}", ""]
    for i, (row, line) in enumerate(hops):
        arrow = "   " if i == 0 else "-> "
        out.append(f"{arrow}{row['qname']}  "
                   f"({row['path']}:{line or row['start_line']})")
    return "\n".join(out)


@mcp.tool()
@graph_tool
def codeorbit_audit(root, conn, severity: str = "medium", limit: int = 20) -> str:
    """Security, bug and quality findings, ranked by real risk.

    Ordered by severity, then by how much code each finding reaches, then by
    whether any test covers it - so the top of the list is what to fix first,
    not merely what appears earliest in the file. severity is high, medium or low.
    """
    if severity not in ("high", "medium", "low"):
        severity = "medium"
    report = auditmod.audit_project(root, conn, min_severity=severity)
    if not report.findings:
        return (f"No findings at severity '{severity}' or above across "
                f"{report.files_scanned} files.")
    out = [f"{len(report.findings)} finding(s) in {report.files_scanned} "
           "files, ordered by severity then by how much code each reaches:", ""]
    for f in report.findings[:limit]:
        out.append(
            f"[{f.rule.severity.upper()}] {f.rule.title}\n"
            f"  {f.path}:{f.line}\n"
            f"  code:    {f.snippet}\n"
            f"  concern: {f.rule.why}\n"
            f"  inside:  {f.symbol or 'module level'} "
            f"(reaches {f.blast} symbol(s), "
            f"{'tested' if f.tested else 'NO test covers it'})")
    return _clip("\n\n".join(out))


@mcp.tool()
@graph_tool
def codeorbit_overview(root, conn) -> str:
    """What this project is made of, and where to start reading.

    Size, the symbols the most code depends on, and definitions nothing calls.
    A good first call on an unfamiliar codebase.
    """
    st = db.stats(conn)
    out = [
        f"# {root.name}",
        f"{st['files']} files, {st['loc']:,} lines, {st['nodes']} symbols, "
        f"{st['edges']} edges", "",
        "## Most depended-on (start here)",
    ]
    entries = query.entry_points(conn, limit=15)
    if entries:
        out += [f"  {r['fan_in']:>4} callers  {r['qname']}  "
                f"({r['path']}:{r['start_line']})" for r in entries]
    else:
        out.append("  (no call edges resolved yet)")

    dead = query.dead_code(conn, limit=10)
    if dead:
        out += ["", "## Nothing in the project calls these",
                "  (may still be entry points, or reached by dynamic dispatch)"]
        out += [f"  {r['qname']}  ({r['path']}:{r['start_line']})" for r in dead]
    return _clip("\n".join(out))


def serve(root: Path) -> None:
    """Run the server on stdio. Blocks until the client disconnects."""
    global DEFAULT_ROOT
    DEFAULT_ROOT = root.resolve()
    log(f"serving graph for {DEFAULT_ROOT}")
    mcp.run(transport="stdio")
