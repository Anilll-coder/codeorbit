"""SQLite storage for the code graph."""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

SCHEMA = Path(__file__).with_name("schema.sql")
DB_DIRNAME = ".codeorbit"
DB_FILENAME = "graph.db"


def db_path(root: Path) -> Path:
    return root / DB_DIRNAME / DB_FILENAME


def connect(root: Path, create: bool = False) -> sqlite3.Connection:
    """Open the graph for `root`. With create=False, a missing index is an error."""
    p = db_path(root)
    if not p.exists() and not create:
        raise FileNotFoundError(
            f"No CodeOrbit index for {root}. Run: codeorbit index {root}"
        )
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA.read_text(encoding="utf-8"))
    return conn


def reset_file(conn: sqlite3.Connection, path: str) -> None:
    """Drop a file and everything derived from it, so re-indexing is idempotent.

    ON DELETE CASCADE clears nodes/edges/imports/pending_calls that hang off it.
    Edges *into* this file's nodes from elsewhere die with their dst node, which
    is why `resolve` must re-run after any re-index.
    """
    conn.execute("DELETE FROM files WHERE path = ?", (path,))


def upsert_file(conn: sqlite3.Connection, path: str, lang: str, sha: str, loc: int) -> int:
    cur = conn.execute(
        "INSERT INTO files(path, lang, hash, loc, indexed_at) VALUES(?,?,?,?,?)",
        (path, lang, sha, loc, time.time()),
    )
    return int(cur.lastrowid)


def insert_node(conn, *, name, qname, kind, file_id, start_line, end_line,
                signature=None, docstring=None) -> int:
    cur = conn.execute(
        """INSERT INTO nodes(name, qname, kind, file_id, start_line, end_line, signature, docstring)
           VALUES(?,?,?,?,?,?,?,?)""",
        (name, qname, kind, file_id, start_line, end_line, signature, docstring),
    )
    nid = int(cur.lastrowid)
    conn.execute(
        "INSERT INTO nodes_fts(rowid, name, qname, docstring) VALUES(?,?,?,?)",
        (nid, name, qname, docstring or ""),
    )
    return nid


def insert_edge(conn, src: int, dst: int, kind: str, line=None, resolution="exact") -> None:
    conn.execute(
        """INSERT OR IGNORE INTO edges(src, dst, kind, line, resolution)
           VALUES(?,?,?,?,?)""",
        (src, dst, kind, line, resolution),
    )


def stats(conn) -> dict:
    q = lambda s: conn.execute(s).fetchone()[0]
    return {
        "files": q("SELECT count(*) FROM files"),
        "nodes": q("SELECT count(*) FROM nodes"),
        "edges": q("SELECT count(*) FROM edges"),
        "loc": q("SELECT coalesce(sum(loc),0) FROM files"),
        "unresolved": q("SELECT count(*) FROM pending_calls WHERE resolved = 0"),
        "resolved": q("SELECT count(*) FROM pending_calls WHERE resolved = 1"),
    }
