"""Turn a question into grounded context for the model.

This module is the whole argument of the project. A small local model cannot
reason its way out of bad context, so the graph - not the model - does the work:
we pick the symbols the question is about, then pull in their *neighbourhood*
(who calls them, what they call) because that is what a reader would need to
answer, and it is exactly what plain text search cannot give you.

Naive RAG over code retrieves chunks that look like the question. This retrieves
the subgraph the question lives in.
"""
from __future__ import annotations

import re
from pathlib import Path

from . import query

STOP = {
    "the", "a", "an", "is", "are", "was", "were", "do", "does", "did", "how",
    "what", "where", "when", "why", "which", "who", "to", "of", "in", "on",
    "for", "and", "or", "it", "this", "that", "with", "from", "by", "at",
    "be", "can", "will", "would", "should", "if", "then", "get", "set",
    "code", "function", "class", "method", "file", "project", "codebase",
    "work", "works", "used", "use", "uses", "call", "calls", "called",
}

SYSTEM = (
    "You are a code analysis assistant. You are given a question and verbatim "
    "source code retrieved from a knowledge graph of the user's repository, "
    "together with the call relationships between those symbols.\n"
    "Rules:\n"
    "1. Answer ONLY from the provided context. Never invent a function, file or "
    "behaviour that is not shown.\n"
    "2. Cite concrete symbols and file:line when you refer to code.\n"
    "3. If the context does not contain the answer, say exactly what is missing "
    "instead of guessing.\n"
    "4. Be concise and concrete. No preamble."
)


def keywords(question: str) -> list[str]:
    """Identifier-ish tokens from the question, most specific first.

    Anything written like code (snake_case, camelCase, dotted, quoted) is treated
    as a literal symbol name and ranked above ordinary prose words.
    """
    literal = re.findall(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+", question)
    coded = re.findall(r"\b[a-z]+_[a-z_0-9]+\b|\b[a-z]+[A-Z][A-Za-z0-9]*\b", question)
    words = [w for w in re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", question)
             if w.lower() not in STOP]
    out, seen = [], set()
    for w in literal + coded + words:
        k = w.strip(".")
        if k and k.lower() not in seen:
            seen.add(k.lower())
            out.append(k)
    return out[:8]


RRF_K = 20          # rank offset; smaller means the top of each list counts more
KEYWORD_WEIGHT = 1.0
SEMANTIC_WEIGHT = 1.0


def keyword_ranking(conn, question: str, limit: int = 12) -> list[int]:
    """Node ids for the question's identifiers, best first."""
    scored: dict[int, float] = {}
    for rank, kw in enumerate(keywords(question)):
        weight = 10 - rank
        for hit_rank, row in enumerate(query.search(conn, kw, limit=8)):
            s = weight * 2 + max(0, 8 - hit_rank)
            if row["kind"] == "module":
                s -= 4              # prefer real definitions over whole files
            scored[row["id"]] = scored.get(row["id"], 0) + s
    return [nid for nid, _ in sorted(scored.items(), key=lambda p: -p[1])][:limit]


def retrieve(conn, question: str, max_symbols: int = 6, semantic: bool = True):
    """Rank symbols for a question by fusing an identifier search with a meaning search.

    The two searches produce scores on scales that cannot be compared. Keyword
    scores run 0-30 and are spiky; cosine similarities from nomic-embed-text sit
    in a narrow 0.55-0.65 band, so adding a multiple of the similarity applies a
    nearly constant offset that ranks nothing and merely lets the keyword scores
    win. That was measured, not assumed: an additive blend returned the
    keyword-only answer for every question tried.

    Reciprocal Rank Fusion avoids the problem by discarding the scores and using
    only each list's ORDER, which is the part that is actually meaningful:

        score(d) = sum over lists of  weight / (K + rank(d))

    A symbol that both lists rank highly beats one that only a single list
    likes, a question naming a real identifier is still won by the keyword list,
    and a question naming none ("where do we decide which files to skip") is
    carried entirely by the semantic list instead of being drowned.
    """
    lists: list[tuple[list[int], float]] = [
        (keyword_ranking(conn, question, max_symbols * 3), KEYWORD_WEIGHT)
    ]

    if semantic:
        try:
            from . import semantic as sem
            if sem.available(conn):
                hits = sem.search(conn, question, limit=max_symbols * 3)
                lists.append(([nid for _, nid in hits], SEMANTIC_WEIGHT))
        except Exception:
            pass    # embeddings are an enhancement; never fail retrieval over them

    fused: dict[int, float] = {}
    for ids, weight in lists:
        for rank, nid in enumerate(ids):
            fused[nid] = fused.get(nid, 0.0) + weight / (RRF_K + rank + 1)

    out = []
    for nid, _ in sorted(fused.items(), key=lambda p: -p[1])[:max_symbols]:
        row = query.get_node(conn, nid)
        if row is not None:
            out.append(row)
    return out


def build(root: Path, conn, question: str, max_symbols: int = 2,
          body_lines: int = 55, semantic: bool = True,
          neighbours: bool = True, picks: list | None = None) -> tuple[str, list]:
    """Return (context_text, symbols_used).

    Bodies are budgeted rather than uniform, and the budget tapers down the
    ranking. A local 3.8B model has a small window and every line spent on the
    fourth-best match is a line the best match does not get - so the symbol
    most likely to hold the answer is the one that arrives complete.

    `neighbours=False` drops the callers/callees/members lines. Nothing in the
    product turns it off; it exists so the benchmark can measure what graph
    expansion contributes with retrieval held fixed.

    `picks` (node rows, best first) replaces retrieval, so a different ranker
    can be given the same graph context - again for the benchmark.
    """
    from . import budget as budgetmod

    if picks is None:
        picks = retrieve(conn, question, max_symbols, semantic=semantic)
    else:
        picks = picks[:max_symbols]
    if not picks:
        return "", []

    budgets = budgetmod.allocate(len(picks), body_lines, floor=15)
    seen_spans: list[tuple[str, int, int]] = []

    parts: list[str] = []
    for row, lines in zip(picks, budgets):
        # A class and one of its own methods are both plausible picks, and
        # their bodies overlap exactly. Printing the inner one again spends
        # context on code already above it.
        span = (row["path"], row["start_line"], min(row["end_line"],
                                                    row["start_line"] + lines))
        covered = any(p == span[0] and s <= span[1] and span[2] <= e
                      for p, s, e in seen_spans)
        body = "" if covered else query.source_of(root, row, max_lines=lines)

        head = (
            f"### {row['kind']} {row['qname']}\n"
            f"defined at {row['path']}:{row['start_line']}"
            f"-{row['end_line']} ({row['lang']})"
        )
        # Signature and docstring only when the source below will not already
        # show them - the def line and a Python docstring are inside the body.
        if row["signature"] and row["signature"].strip() not in body:
            head += f"\nsignature: {row['signature']}"
        if row["docstring"] and row["docstring"].strip()[:60] not in body:
            head += f"\ndoc: {row['docstring'][:280]}"
        parts.append(head)

        ins = (group_neighbours(query.callers(conn, row["id"], limit=24), 6)
               if neighbours else [])
        if ins:
            parts.append("called by:\n" + "\n".join(
                f"  - {c['qname']} ({c['path']}:{call_lines(c)})"
                f"{' [uncertain]' if c['resolution'] != 'exact' else ''}"
                for c in ins
            ))

        outs = (group_neighbours(query.callees(conn, row["id"], limit=32), 8)
                if neighbours else [])
        if outs:
            parts.append("calls:\n" + "\n".join(
                f"  - {c['qname']} (line {call_lines(c)})"
                f"{' [uncertain]' if c['resolution'] != 'exact' else ''}"
                for c in outs
            ))

        if neighbours and row["kind"] == "class":
            mem = query.members(conn, row["id"])
            if mem:
                parts.append("members: " + ", ".join(m["name"] for m in mem[:20]))

        if not covered:
            seen_spans.append(span)
            if body:
                parts.append(f"source:\n```{row['lang']}\n"
                             f"{budgetmod.trim_source(body)}\n```")

        parts.append("")

    return "\n".join(parts), picks


def group_neighbours(rows, limit: int) -> list[dict]:
    """One entry per neighbour, with all its call lines.

    The graph has an edge per call SITE, so a function called three times was
    listed three times - the same name and path paid for again on every line.
    """
    out: dict[tuple, dict] = {}
    for r in rows:
        key = (r["qname"], r["path"])
        if key not in out:
            if len(out) == limit:
                continue
            out[key] = {"qname": r["qname"], "path": r["path"], "lines": [],
                        "resolution": r["resolution"]}
        out[key]["lines"].append(r["call_line"])
        if r["resolution"] == "exact":
            out[key]["resolution"] = "exact"
    return list(out.values())


def call_lines(c: dict) -> str:
    return ", ".join(str(n) for n in sorted(set(c["lines"])))


def format_outline(path: str, rows) -> str:
    """A file's definitions - line, kind, signature - and none of its code.

    What is in a file costs a few hundred tokens this way; reading the file to
    find out costs tens of thousands on a large one.
    """
    out = [f"{path}: {len(rows)} definition(s)"]
    for r in rows:
        label = r["signature"] or r["name"]
        if r["name"] not in label:
            label = f"{r['name']}{label}"
        indent = "  " if r["kind"] == "method" else ""
        out.append(f"{r['start_line']:>5}-{r['end_line']:<5} {indent}{r['kind']} "
                   f"{' '.join(label.split())[:140]}")
    return "\n".join(out)


def prompt_for(question: str, context: str) -> str:
    return (
        "Repository context retrieved from the code graph:\n\n"
        f"{context}\n"
        "----------------------------------------\n"
        f"Question: {question}\n\n"
        "Answer using only the context above, citing symbols and file:line."
    )
