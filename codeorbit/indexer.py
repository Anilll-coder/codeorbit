"""Pass 1: parse every source file into nodes, imports and pending call sites."""
from __future__ import annotations

from pathlib import Path

from . import db
from .extract import parse_and_extract
from .scanner import scan, sha1


def index_project(root: Path, progress=None) -> dict:
    root = root.resolve()
    conn = db.connect(root, create=True)
    files = scan(root)

    conn.execute("DELETE FROM files")      # full re-index
    conn.execute("DELETE FROM nodes_fts")

    total_syms = 0
    for i, (path, lang) in enumerate(files, 1):
        rel = path.relative_to(root).as_posix()
        try:
            raw = path.read_bytes()
        except OSError:
            continue

        try:
            ex = parse_and_extract(raw, lang, rel)
        except Exception:
            continue  # a file that will not parse must not kill the whole index

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
        total_syms += len(ex.symbols)

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

        if progress:
            progress(i, len(files), rel)

    conn.commit()
    st = db.stats(conn)
    conn.close()
    st["scanned"] = len(files)
    st["symbols"] = total_syms
    return st
