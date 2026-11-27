"""Keep the graph in step with the files while an agent edits them.

The graph is built once, by `codeorbit index`, but an agent in a long chat
edits files between calls. Source is read from disk at query time, while line
ranges come from the index, so after an edit a stale graph prints the wrong
lines, misses new functions, and tells the agent a body is "unchanged since"
when it is not. Grep never has this problem; CodeOrbit must not have it either.

Checking has to be cheap, because it runs before every tool call: a stat of
each indexed file (mtime and size) and of each directory holding one (a new or
deleted file changes its directory's mtime). Only when that differs does the
real, incremental `index_project` run - it re-parses just the files whose
contents changed - followed by `resolve_project` if anything did.
"""
from __future__ import annotations

import os
import threading
from pathlib import Path

from . import db
from .indexer import index_project
from .resolve import resolve_project

_LOCK = threading.Lock()
_SNAP: dict[Path, dict] = {}


def _snapshot(root: Path, paths: list[str]) -> dict:
    snap: dict = {}
    dirs = {""}
    for rel in paths:
        try:
            st = os.stat(root / rel)
            snap[("f", rel)] = (st.st_mtime_ns, st.st_size)
        except OSError:
            snap[("f", rel)] = None
        d = rel.rpartition("/")[0]
        while d not in dirs:
            dirs.add(d)
            d = d.rpartition("/")[0]
    for d in dirs:
        try:
            snap[("d", d)] = os.stat(root / d).st_mtime_ns
        except OSError:
            snap[("d", d)] = None
    return snap


def _hashes(root: Path) -> dict[str, str]:
    conn = db.connect(root)
    try:
        return {r["path"]: r["hash"] for r in conn.execute("SELECT path, hash FROM files")}
    finally:
        conn.close()


def _built_at(root: Path) -> int:
    """When the graph was last written (the database or its WAL)."""
    p = db.db_path(root)
    times = []
    for f in (p, p.with_name(p.name + "-wal")):
        try:
            times.append(f.stat().st_mtime_ns)
        except OSError:
            pass
    return max(times, default=0)


def _newer_than(snap: dict, t: int) -> bool:
    for v in snap.values():
        if v is None or (v[0] if isinstance(v, tuple) else v) > t:
            return True
    return False


def refresh(root: Path) -> list[str]:
    """Bring the graph up to date with the files on disk.

    Returns the paths whose contents changed, were added or were removed since
    the last call (or since the index was built, on the first call), so that
    anything already sent from them can be sent again.
    """
    root = root.resolve()
    with _LOCK:
        try:
            before = _hashes(root)
        except FileNotFoundError:
            return []
        snap = _snapshot(root, list(before))
        prev = _SNAP.get(root)
        dirty = _newer_than(snap, _built_at(root)) if prev is None else snap != prev
        if not dirty:
            _SNAP[root] = snap
            return []
        st = index_project(root)
        if st["touched"]:
            resolve_project(root)
        after = _hashes(root)
        _SNAP[root] = _snapshot(root, list(after))
        return sorted(p for p in before.keys() | after.keys()
                      if before.get(p) != after.get(p))


def forget(root: Path | None = None) -> None:
    """Drop the remembered snapshot (tests; a root that was re-indexed by hand)."""
    with _LOCK:
        if root is None:
            _SNAP.clear()
        else:
            _SNAP.pop(root.resolve(), None)
