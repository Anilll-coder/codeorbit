"""Pass 1: parse source files into nodes, imports and pending call sites.

Incremental by default. Each file's SHA-1 is stored with it, so a re-index only
re-parses files whose contents actually changed, plus files that vanished. On a
codebase of any size that is the difference between a command you run and one
you avoid - and it is what makes the tool usable in a save-then-ask loop.

Deletion is by file row: everything derived from a file hangs off it with
ON DELETE CASCADE, so dropping the row drops its nodes, imports and pending
calls. Edges INTO those nodes from elsewhere die with them, which is why
resolve must always run after an index that changed anything.
"""
from __future__ import annotations

from pathlib import Path

from . import db
from .extract import parse_and_extract
from .scanner import scan, sha1


def _index_one(conn, root: Path, path: Path, rel: str, lang: str) -> int:
    """Parse one file into the graph. Returns the symbol count."""
    try:
        raw = path.read_bytes()
    except OSError:
        return 0

    try:
        ex = parse_and_extract(raw, lang, rel)
    except Exception:
        return 0  # a file that will not parse must not kill the whole index

    loc = raw.count(b"\n") + 1
    file_id = db.upsert_file(conn, rel, lang, sha1(raw), loc)

    # qname -> node id, so contains-edges and call sites bind within the file
    ids: dict[str, int] = {}
    for s in ex.symbols:
        ids[s.qname] = db.insert_node(
            conn, name=s.name, qname=s.qname, kind=s.kind, file_id=file_id,
            start_line=s.start_line, end_line=s.end_line,
            signature=s.signature, docstring=s.docstring,
        )

    for s in ex.symbols:
        if s.parent and s.parent in ids and s.qname in ids:
            db.insert_edge(conn, ids[s.parent], ids[s.qname], "contains")

    for imp in ex.imports:
        conn.execute(
            "INSERT INTO imports(file_id, module, symbol, alias, line) VALUES(?,?,?,?,?)",
            (file_id, imp.module, imp.symbol, imp.alias, imp.line),
        )

    for c in ex.calls:
        src_id = ids.get(c.caller_qname)
        if src_id is None:
            continue
        conn.execute(
            "INSERT INTO pending_calls(src, callee, recv, file_id, line) VALUES(?,?,?,?,?)",
            (src_id, c.callee, c.recv, file_id, c.line),
        )

    # extends is recorded by name now, bound to a real node during resolve
    for cls_q, base in ex.base_types:
        src_id = ids.get(cls_q)
        if src_id is not None:
            conn.execute(
                "INSERT INTO pending_calls(src, callee, recv, file_id, line) VALUES(?,?,?,?,?)",
                (src_id, base, "__extends__", file_id, 0),
            )

    return len(ex.symbols)


def index_project(root: Path, progress=None, full: bool = False) -> dict:
    """Index `root`. Re-parses only changed files unless `full` is set."""
    root = root.resolve()
    conn = db.connect(root, create=True)
    found = scan(root)

    if full:
        conn.execute("DELETE FROM files")
        conn.execute("DELETE FROM nodes_fts")
        known: dict[str, str] = {}
    else:
        known = {
            r["path"]: r["hash"]
            for r in conn.execute("SELECT path, hash FROM files")
        }

    on_disk = {p.relative_to(root).as_posix(): (p, lang) for p, lang in found}

    # Files that disappeared since the last index.
    removed = [rel for rel in known if rel not in on_disk]
    for rel in removed:
        db.reset_file(conn, rel)

    added = changed = unchanged = 0
    total_syms = 0

    for i, (rel, (path, lang)) in enumerate(sorted(on_disk.items()), 1):
        prior = known.get(rel)
        if prior is not None:
            try:
                if sha1(path.read_bytes()) == prior:
                    unchanged += 1
                    if progress:
                        progress(i, len(on_disk), rel)
                    continue
            except OSError:
                continue
            db.reset_file(conn, rel)     # contents moved on; drop and re-parse
            changed += 1
        else:
            added += 1

        total_syms += _index_one(conn, root, path, rel, lang)
        if progress:
            progress(i, len(on_disk), rel)

    db.rebuild_fts(conn)
    conn.commit()
    st = db.stats(conn)
    conn.close()

    st.update({
        "scanned": len(on_disk),
        "symbols": total_syms,
        "added": added,
        "changed": changed,
        "removed": len(removed),
        "unchanged": unchanged,
        "touched": added + changed + len(removed),
    })
    return st
