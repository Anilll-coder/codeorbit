"""Read-side queries over the graph: search, neighbourhood, impact, source."""
from __future__ import annotations

import sqlite3
from collections import deque
from pathlib import Path

from . import db

NODE_COLS = (
    "n.id, n.name, n.qname, n.kind, n.start_line, n.end_line, "
    "n.signature, n.docstring, f.path AS path, f.lang AS lang"
)


def _rows(conn, sql, args=()):
    return conn.execute(sql, args).fetchall()


def search(conn, term: str, limit: int = 20) -> list[sqlite3.Row]:
    """Full-text search, with an exact-name match always floated to the top."""
    exact = _rows(
        conn,
        f"SELECT {NODE_COLS} FROM nodes n JOIN files f ON f.id = n.file_id "
        "WHERE n.name = ? ORDER BY (n.kind='module') LIMIT ?",
        (term, limit),
    )
    seen = {r["id"] for r in exact}
    out = list(exact)
    if len(out) < limit:
        try:
            fts = _rows(
                conn,
                f"SELECT {NODE_COLS} FROM nodes_fts "
                "JOIN nodes n ON n.id = nodes_fts.rowid "
                "JOIN files f ON f.id = n.file_id "
                "WHERE nodes_fts MATCH ? ORDER BY rank LIMIT ?",
                (term, limit),
            )
        except sqlite3.OperationalError:
            fts = []
        for r in fts:
            if r["id"] not in seen:
                out.append(r)
                seen.add(r["id"])
    if len(out) < limit:
        like = _rows(
            conn,
            f"SELECT {NODE_COLS} FROM nodes n JOIN files f ON f.id = n.file_id "
            "WHERE n.qname LIKE ? LIMIT ?",
            (f"%{term}%", limit),
        )
        for r in like:
            if r["id"] not in seen:
                out.append(r)
                seen.add(r["id"])
    return out[:limit]


def get_node(conn, node_id: int):
    r = _rows(
        conn,
        f"SELECT {NODE_COLS} FROM nodes n JOIN files f ON f.id = n.file_id WHERE n.id = ?",
        (node_id,),
    )
    return r[0] if r else None


def callers(conn, node_id: int, limit: int = 25):
    return _rows(
        conn,
        f"SELECT {NODE_COLS}, e.line AS call_line, e.resolution "
        "FROM edges e JOIN nodes n ON n.id = e.src JOIN files f ON f.id = n.file_id "
        "WHERE e.dst = ? AND e.kind = 'calls' "
        "ORDER BY (e.resolution='exact') DESC LIMIT ?",
        (node_id, limit),
    )


def callees(conn, node_id: int, limit: int = 25):
    return _rows(
        conn,
        f"SELECT {NODE_COLS}, e.line AS call_line, e.resolution "
        "FROM edges e JOIN nodes n ON n.id = e.dst JOIN files f ON f.id = n.file_id "
        "WHERE e.src = ? AND e.kind = 'calls' "
        "ORDER BY e.line LIMIT ?",
        (node_id, limit),
    )


def members(conn, node_id: int):
    return _rows(
        conn,
        f"SELECT {NODE_COLS} FROM edges e JOIN nodes n ON n.id = e.dst "
        "JOIN files f ON f.id = n.file_id "
        "WHERE e.src = ? AND e.kind = 'contains' ORDER BY n.start_line",
        (node_id,),
    )


def impact(conn, node_id: int, max_depth: int = 3) -> list[tuple[int, sqlite3.Row]]:
    """Everything that transitively calls this symbol - the blast radius.

    BFS over reverse `calls` edges. Depth is capped because a utility function
    reaches the whole project and an unbounded answer is not an answer.
    """
    seen = {node_id}
    out: list[tuple[int, sqlite3.Row]] = []
    frontier = deque([(node_id, 0)])
    while frontier:
        nid, d = frontier.popleft()
        if d >= max_depth:
            continue
        for r in callers(conn, nid, limit=200):
            if r["id"] in seen:
                continue
            seen.add(r["id"])
            out.append((d + 1, r))
            frontier.append((r["id"], d + 1))
    return out


def affected_tests(conn, node_id: int, max_depth: int = 4) -> list[sqlite3.Row]:
    """Of the blast radius, the parts that look like tests."""
    hits = []
    for _, r in impact(conn, node_id, max_depth):
        p = (r["path"] or "").lower()
        nm = (r["name"] or "").lower()
        if "test" in p or "spec" in p or nm.startswith("test"):
            hits.append(r)
    return hits


def source_of(root: Path, row, max_lines: int = 200) -> str:
    """The verbatim source of a symbol, line-numbered."""
    fp = root / row["path"]
    try:
        lines = fp.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    start = max(row["start_line"] - 1, 0)
    end = min(row["end_line"], start + max_lines)
    width = len(str(end))
    return "\n".join(
        f"{i + 1:>{width}} | {lines[i]}" for i in range(start, min(end, len(lines)))
    )


def entry_points(conn, limit: int = 15):
    """Symbols the most code depends on - a decent 'start here' list."""
    return _rows(
        conn,
        f"SELECT {NODE_COLS}, count(e.id) AS fan_in "
        "FROM nodes n JOIN files f ON f.id = n.file_id "
        "JOIN edges e ON e.dst = n.id AND e.kind = 'calls' "
        "WHERE n.kind != 'module' "
        "GROUP BY n.id ORDER BY fan_in DESC LIMIT ?",
        (limit,),
    )


def dead_code(conn, limit: int = 30):
    """Definitions nothing in the project calls or contains-references."""
    return _rows(
        conn,
        f"SELECT {NODE_COLS} FROM nodes n JOIN files f ON f.id = n.file_id "
        "WHERE n.kind IN ('function','method','class') "
        "AND NOT EXISTS (SELECT 1 FROM edges e WHERE e.dst = n.id AND e.kind = 'calls') "
        "AND n.name NOT LIKE '\\_\\_%' ESCAPE '\\' "
        "ORDER BY (n.end_line - n.start_line) DESC LIMIT ?",
        (limit,),
    )


def open_graph(root: Path):
    return db.connect(root)
