"""The lean MCP profile: CodeOrbit at grep's price.

Measured on Claude Code with Sonnet (bench/agent_runs/*-claude-sonnet), the
full profile cost a strong model MORE input tokens than grep alone, with the
same accuracy, for two reasons:

  1. Every request carries every tool's definition. Five tools plus the server
     instructions are ~800 tokens, paid again on every turn.
  2. explore's first answer is 1.5-2k tokens of source, where a strong model's
     grep is 200-1,400 - and whatever a tool returns is re-read on every later
     turn. When it did not settle the question the model grepped and read
     anyway, and paid for both.

So this profile has two tools with one-line descriptions and no optional
parameters, and `find` answers in grep's shape: ranked locations, each with
its signature and the few lines that match the question, no bodies. The graph
still does what grep cannot - rank whole functions by the question and its
call structure - but the answer is as small as a grep hit list. Bodies come
from `show`, one function at a time, only when asked for.
"""
from __future__ import annotations

from mcp.server import MCPServer

from . import context as ctxmod
from . import mcp_server as ms
from . import query

INSTRUCTIONS = ("CodeOrbit: ranked code search over this repo's call graph. "
                "codeorbit_find answers like grep but ranks whole functions by "
                "the question; codeorbit_show gives one function's code.")

FIND_RESULTS = 6           # locations listed
FIND_LINES = (4, 2)        # matching lines shown: the best match, each other one
LINE_CHARS = 100
SIG_CHARS = 90
SHOW_LINES = 60
SHOW_NEIGHBOURS = (4, 4)

lean = MCPServer(name="codeorbit", instructions=INSTRUCTIONS)


def _lean_tool(fn):
    """graph_tool, minus the optional project_path parameter: a definition
    is paid for on every turn, and the server already knows its project."""
    wrapped = ms.graph_tool(fn)
    sig = wrapped.__signature__
    wrapped.__signature__ = sig.replace(parameters=[
        p for p in sig.parameters.values() if p.name != "project_path"])
    return wrapped


def _terms(text: str) -> set[str]:
    from .rank import code_tokens
    return {t for t in code_tokens(text) if t not in ctxmod.STOP and len(t) > 2}


def _matching_lines(root, row, terms: set[str], k: int) -> list[str]:
    """The k body lines that name the most question words, in file order.
    The signature's own lines are skipped: the header already gives it."""
    span = row["end_line"] - row["start_line"] + 1
    src = query.source_of(root, row, max_lines=min(span, 400))
    sig = " ".join((row["signature"] or "").split()).lower()
    lines = src.split("\n")[1:] if src else []
    scored = []
    for i, line in enumerate(lines):
        code = line.split("|", 1)[-1].lower()
        if " ".join(code.split()) in sig:
            continue                    # a parameter line: the header has it
        hit = sum(1 for t in terms if t in code)
        if hit:
            scored.append((-hit, i))
    keep = sorted(i for _, i in sorted(scored)[:k])
    out = []
    for i in keep:
        num, _, code = lines[i].partition("|")
        code = code.strip()
        out.append(f"    {num.strip()}: {code[:LINE_CHARS]}{'…' if len(code) > LINE_CHARS else ''}")
    return out


def _short(qname: str, path: str) -> str:
    """The name without the module the path already says."""
    mod = path.rsplit(".", 1)[0].replace("/", ".")
    for m in (mod, mod.removesuffix(".__init__")):
        if qname.startswith(m + "."):
            return qname[len(m) + 1:]
    return qname


def _sig(row) -> str:
    s = " ".join((row["signature"] or "").split()).replace("( ", "(").replace(" )", ")")
    return s if len(s) <= SIG_CHARS else s[:SIG_CHARS] + "…"


def find_text(root, conn, query_text: str) -> str:
    seen = ms.LEDGER.shown
    rows = [r for r in ms.explore_rows(root, conn, query_text, FIND_RESULTS + len(seen))
            if r["id"] not in seen][:FIND_RESULTS]
    if not rows:
        if seen:
            return ("Everything matching this was already listed: "
                    + ", ".join(list(seen.values())[:FIND_RESULTS]))
        return f"No match for {query_text!r}. Try names or words from the code."
    terms = _terms(query_text)
    out, last = [], None
    for n, r in enumerate(rows):
        if r["path"] != last:
            out.append(r["path"])
            last = r["path"]
        name = _short(r["qname"], r["path"])
        sig = _sig(r) if r["kind"] not in ("class", "module") else ""
        if sig.startswith(r["name"]):
            sig = sig[len(r["name"]):]          # "name(...)": the name is already there
        out.append(f"  {r['start_line']}-{r['end_line']} {r['kind']} {name}{sig}")
        if r["kind"] not in ("class", "module"):
            out += _matching_lines(root, r, terms, FIND_LINES[0] if n == 0 else FIND_LINES[1])
        ms.LEDGER.note_shown(r["id"], r["qname"], r["path"])
    return "\n".join(out)


@lean.tool()
@_lean_tool
def codeorbit_find(root, conn, query: str) -> str:
    """Rank the functions an issue or question is about: file:lines, signature, matching lines."""
    return find_text(root, conn, query)


@lean.tool()
@_lean_tool
def codeorbit_show(root, conn, name: str) -> str:
    """One function's source, callers and callees."""
    hits = ms.query.search(conn, name, limit=4)
    if not hits:
        return f"No symbol named {name!r}; codeorbit_find lists names."
    ledger = ms._ledger_for("auto", [hits[0]["id"]])
    text = ms._fmt_symbol(root, conn, hits[0], body_lines=SHOW_LINES, ledger=ledger,
                          neighbours=SHOW_NEIGHBOURS, focus=None)
    if len(hits) > 1:
        text += "\nSame name: " + ", ".join(f"{h['qname']} ({h['path']})" for h in hits[1:])
    return ms._clip(text)


def serve(root) -> None:
    ms.DEFAULT_ROOT = root.resolve()
    ms.log(f"serving graph (lean) for {ms.DEFAULT_ROOT}")
    lean.run(transport="stdio")
