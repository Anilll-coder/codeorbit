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
from . import context as ctxmod
from . import db, query
from .db import DB_DIRNAME

# Set from the CLI; any tool may override it per-call with project_path.
DEFAULT_ROOT: Path = Path.cwd()

MAX_BODY_LINES = 120
MAX_CHARS = 24_000          # keep one response inside a sane slice of context

INSTRUCTIONS = """\
CodeOrbit serves a structural knowledge graph of this repository: its symbols,
their call relationships, and the real source behind them.

Prefer codeorbit_explore over reading files. It returns verbatim source together
with who calls each symbol and what it calls - treat what comes back as already
read, and do not re-open those files.

Typical order: codeorbit_overview on an unfamiliar project, codeorbit_explore
for any "how does X work" question, codeorbit_node for one symbol in full,
codeorbit_impact before changing something, codeorbit_path for "how does A reach
B".

Edges marked [uncertain] were resolved by name rather than by an import, so
treat them as likely rather than certain. Static parsing cannot see dynamic
dispatch, so a missing edge means "not statically visible", never "does not
happen".
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


def _fmt_symbol(root: Path, conn, row, body_lines: int = MAX_BODY_LINES) -> str:
    out = [
        f"## {row['kind']} {row['qname']}",
        f"{row['path']}:{row['start_line']}-{row['end_line']}  ({row['lang']})",
    ]
    if row["signature"]:
        out.append(f"signature: {row['signature']}")
    if row["docstring"]:
        out.append(f"doc: {row['docstring'][:400]}")

    callers = query.callers(conn, row["id"], limit=10)
    if callers:
        out.append("\ncalled by:")
        for c in callers:
            flag = "" if c["resolution"] == "exact" else "  [uncertain]"
            out.append(f"  {c['qname']}  ({c['path']}:{c['call_line']}){flag}")
    else:
        out.append("\ncalled by: nothing in this project")

    callees = query.callees(conn, row["id"], limit=12)
    if callees:
        out.append("\ncalls:")
        for c in callees:
            flag = "" if c["resolution"] == "exact" else "  [uncertain]"
            out.append(f"  {c['qname']}  (line {c['call_line']}){flag}")

    if row["kind"] == "class":
        members = query.members(conn, row["id"])
        if members:
            out.append("\nmembers: " + ", ".join(m["name"] for m in members[:25]))

    src = query.source_of(root, row, max_lines=body_lines)
    if src:
        out.append(f"\n```{row['lang']}\n{src}\n```")

    return "\n".join(out)


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
def codeorbit_explore(root, conn, query_text: str, max_symbols: int = 4) -> str:
    """PRIMARY TOOL. Answer a question about this codebase from its call graph.

    Retrieves the symbols the question is about, their real source, who calls
    them and what they call. Use this FIRST for any "how does X work", "where is
    Y handled" or "what happens when Z" question. Naming concrete symbols
    (including qualified ones like Class.method) sharpens the result. Treat the
    source it returns as already read - do not open those files again.
    """
    picks = ctxmod.retrieve(conn, query_text, max_symbols=max_symbols)
    if not picks:
        return (f"Nothing in the graph matched: {query_text!r}\n\n"
                "Try codeorbit_search with a single identifier to see what "
                "exists, or codeorbit_overview for the project's main symbols.")
    header = f"Retrieved {len(picks)} symbol(s) for: {query_text}\n"
    return _clip(header + "\n\n" + "\n\n".join(
        _fmt_symbol(root, conn, r) for r in picks))


@mcp.tool()
@graph_tool
def codeorbit_search(root, conn, term: str, limit: int = 20) -> str:
    """Find symbols by name. Returns kind, qualified name and file:line.

    Use to locate something, or to disambiguate before calling codeorbit_node.
    """
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
def codeorbit_node(root, conn, name: str) -> str:
    """Everything about one symbol in a single call.

    Full source, its callers, and what it calls. If the name is ambiguous, the
    other candidates are listed too so no follow-up call is needed. Treat the
    returned source as already read.
    """
    hits = query.search(conn, name, limit=6)
    if not hits:
        return (f"No symbol named {name!r}. Call codeorbit_search to find "
                "the right name.")
    parts = [_fmt_symbol(root, conn, hits[0])]
    if len(hits) > 1:
        parts.append("\nOther symbols share this name:")
        parts += [f"  {h['kind']} {h['qname']} ({h['path']}:{h['start_line']})"
                  for h in hits[1:5]]
    return _clip("\n".join(parts))


@mcp.tool()
@graph_tool
def codeorbit_impact(root, conn, name: str, depth: int = 3) -> str:
    """Blast radius: everything that transitively reaches a symbol, plus its tests.

    Use before changing something, or to judge how risky a change is. Reports
    explicitly when no test covers the symbol.
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
                for d, r in reach[:60]]
    else:
        out.append("  Nothing in this project calls it.")
    out.append("")
    if tests:
        out.append(f"Tests that reach it ({len(tests)}):")
        out += [f"  {t['qname']}  ({t['path']})" for t in tests[:25]]
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
