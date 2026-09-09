"""Semantic symbol search, on top of the graph rather than instead of it.

Keyword search answers "where is `parse_and_extract`". It cannot answer "where
do we decide which files to skip" - the words in that question appear nowhere in
the code. Embedding each symbol's name, signature and docstring closes that gap.

This deliberately does NOT embed whole files or arbitrary chunks, which is what
most code-RAG does. The unit is the symbol, because the symbol is what the graph
can then expand: a semantic hit is only the entry point, and the neighbourhood
around it still comes from real call edges. Meaning finds the door; structure
walks the building.

Vectors are float32 in a BLOB and compared in pure Python. At a few thousand
symbols a linear scan takes milliseconds, which is far below the cost of the
generation call - so there is no index to keep correct.
"""
from __future__ import annotations

import math
import struct
from pathlib import Path

from . import llm

BATCH_LOG_EVERY = 25

# nomic-embed-text is trained with task prefixes and expects them: stored text
# is a "document", the thing being asked is a "query". Without these the two
# land in slightly different regions of the space and similarity is measurably
# worse - which is exactly what the first version of this file got wrong.
DOC_PREFIX = "search_document: "
QUERY_PREFIX = "search_query: "


def pack(vec: list[float]) -> bytes:
    return struct.pack(f"<{len(vec)}f", *vec)


def unpack(blob: bytes, dim: int) -> tuple[float, ...]:
    return struct.unpack(f"<{dim}f", blob)


def normalize(vec: list[float]) -> list[float]:
    """Unit-length, so cosine similarity is a plain dot product later."""
    n = math.sqrt(sum(v * v for v in vec))
    return [v / n for v in vec] if n else vec


BODY_CHARS = 700


def symbol_text(row, root: Path | None = None) -> str:
    """What of a symbol we actually embed.

    Name and qualified name carry the vocabulary a developer searches with, the
    signature carries its shape, the docstring carries intent - and a slice of
    the body carries what it actually does, which is what a question like
    "where do we decide which files to skip" is really asking about. An early
    version left the body out on the theory that it would dilute the vector;
    measured, the opposite held: without it every short undocumented helper
    embeds to nearly the same point and semantic search adds nothing.
    """
    parts = [f"{row['kind']} {row['name']}"]
    if row["qname"] and row["qname"] != row["name"]:
        parts.append(row["qname"].replace(".", " "))
    if row["signature"]:
        parts.append(row["signature"])
    if row["docstring"]:
        parts.append(row["docstring"][:400])

    keys = row.keys()
    if root is not None and "path" in keys and "start_line" in keys:
        try:
            lines = (root / row["path"]).read_text(
                encoding="utf-8", errors="replace"
            ).splitlines()
            body = "\n".join(lines[row["start_line"] - 1:row["end_line"]])
            if body.strip():
                parts.append(body[:BODY_CHARS])
        except OSError:
            pass

    return "\n".join(parts)


def pending(conn, model: str) -> list:
    """Symbols with no current embedding for this model."""
    return conn.execute(
        "SELECT n.id, n.name, n.qname, n.kind, n.signature, n.docstring, "
        "n.start_line, n.end_line, f.path AS path "
        "FROM nodes n JOIN files f ON f.id = n.file_id "
        "LEFT JOIN embeddings e ON e.node_id = n.id AND e.model = ? "
        "WHERE n.kind != 'module' AND e.node_id IS NULL "
        "ORDER BY n.id",
        (model,),
    ).fetchall()


def count(conn, model: str | None = None) -> int:
    if model:
        return conn.execute(
            "SELECT count(*) FROM embeddings WHERE model = ?", (model,)
        ).fetchone()[0]
    return conn.execute("SELECT count(*) FROM embeddings").fetchone()[0]


def build(conn, root: Path, model: str = llm.EMBED_MODEL, progress=None) -> dict:
    """Embed every symbol that does not yet have a vector. Resumable."""
    # Vectors whose node is gone, or whose file changed and was re-parsed,
    # cascade away with the node - so this only ever sees live symbols.
    todo = pending(conn, model)
    done = 0
    failed = 0

    for i, row in enumerate(todo, 1):
        try:
            vec = llm.embed([DOC_PREFIX + symbol_text(row, root)], model=model)[0]
        except Exception:
            failed += 1
            continue
        vec = normalize(vec)
        conn.execute(
            "INSERT OR REPLACE INTO embeddings(node_id, dim, vec, model) VALUES(?,?,?,?)",
            (row["id"], len(vec), pack(vec), model),
        )
        done += 1
        if progress and (i % BATCH_LOG_EVERY == 0 or i == len(todo)):
            progress(i, len(todo))
        if done % 200 == 0:
            conn.commit()

    conn.commit()
    return {"embedded": done, "failed": failed, "total": count(conn, model)}


def search(conn, question: str, limit: int = 8,
           model: str = llm.EMBED_MODEL) -> list[tuple[float, int]]:
    """Return [(similarity, node_id)] for the closest symbols, best first."""
    rows = conn.execute(
        "SELECT node_id, dim, vec FROM embeddings WHERE model = ?", (model,)
    ).fetchall()
    if not rows:
        return []

    try:
        q = normalize(llm.embed([QUERY_PREFIX + question], model=model)[0])
    except Exception:
        return []

    scored: list[tuple[float, int]] = []
    for r in rows:
        v = unpack(r["vec"], r["dim"])
        if len(v) != len(q):
            continue                        # a different model's vectors
        scored.append((sum(a * b for a, b in zip(q, v)), r["node_id"]))

    scored.sort(reverse=True)
    return scored[:limit]


def available(conn, model: str = llm.EMBED_MODEL) -> bool:
    try:
        return count(conn, model) > 0
    except Exception:
        return False
