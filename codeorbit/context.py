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


def retrieve(conn, question: str, max_symbols: int = 6):
    """Rank symbols by how well they match the question's identifiers."""
    scored: dict[int, list] = {}
    for rank, kw in enumerate(keywords(question)):
        weight = 10 - rank
        for hit_rank, row in enumerate(query.search(conn, kw, limit=8)):
            prev = scored.get(row["id"])
            score = weight * 2 + max(0, 8 - hit_rank)
            if row["kind"] == "module":
                score -= 4          # prefer real definitions over whole files
            if prev is None:
                scored[row["id"]] = [score, row]
            else:
                prev[0] += score
    ordered = sorted(scored.values(), key=lambda p: -p[0])
    return [row for _, row in ordered[:max_symbols]]


def build(root: Path, conn, question: str, max_symbols: int = 2,
          body_lines: int = 55) -> tuple[str, list]:
    """Return (context_text, symbols_used)."""
    picks = retrieve(conn, question, max_symbols)
    if not picks:
        return "", []

    parts: list[str] = []
    for row in picks:
        head = (
            f"### {row['kind']} {row['qname']}\n"
            f"defined at {row['path']}:{row['start_line']}"
            f"-{row['end_line']} ({row['lang']})"
        )
        if row["signature"]:
            head += f"\nsignature: {row['signature']}"
        if row["docstring"]:
            head += f"\ndoc: {row['docstring'][:280]}"
        parts.append(head)

        ins = query.callers(conn, row["id"], limit=6)
        if ins:
            parts.append("called by:\n" + "\n".join(
                f"  - {c['qname']} ({c['path']}:{c['call_line']})"
                f"{' [uncertain]' if c['resolution'] != 'exact' else ''}"
                for c in ins
            ))

        outs = query.callees(conn, row["id"], limit=8)
        if outs:
            parts.append("calls:\n" + "\n".join(
                f"  - {c['qname']} (line {c['call_line']})"
                f"{' [uncertain]' if c['resolution'] != 'exact' else ''}"
                for c in outs
            ))

        if row["kind"] == "class":
            mem = query.members(conn, row["id"])
            if mem:
                parts.append("members: " + ", ".join(m["name"] for m in mem[:20]))

        body = query.source_of(root, row, max_lines=body_lines)
        if body:
            parts.append(f"source:\n```{row['lang']}\n{body}\n```")

        parts.append("")

    return "\n".join(parts), picks


def prompt_for(question: str, context: str) -> str:
    return (
        "Repository context retrieved from the code graph:\n\n"
        f"{context}\n"
        "----------------------------------------\n"
        f"Question: {question}\n\n"
        "Answer using only the context above, citing symbols and file:line."
    )
