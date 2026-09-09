"""Pass 2: bind pending call sites to the definitions they actually reach.

Resolution order, most trustworthy first:

  1. same file          - a name defined in this file wins outright
  2. self / this        - a receiver of self or this inside a class binds to
                          that class's own method
  3. explicit import    - the file imports that name from a module we indexed
  4. unique global name - exactly one definition project-wide carries the name
  5. ambiguous          - several definitions share the name

Steps 1-3 are recorded as `exact`; 4 and 5 as `heuristic`. Keeping that
distinction is what lets the answer layer say how much it trusts an edge,
instead of presenting a guess as a fact. A name shared by more than
MAX_AMBIGUOUS definitions is dropped rather than linked everywhere - a wrong
edge is worse than a missing one, because the model believes it.
"""
from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from . import db

MAX_AMBIGUOUS = 3


def resolve_project(root: Path) -> dict:
    conn = db.connect(root)

    conn.execute("DELETE FROM edges WHERE kind IN ('calls','extends','imports')")
    conn.execute("UPDATE pending_calls SET resolved = 0")

    nodes = conn.execute(
        "SELECT n.id, n.name, n.qname, n.kind, n.file_id, f.lang AS lang "
        "FROM nodes n JOIN files f ON f.id = n.file_id WHERE n.kind != 'module'"
    ).fetchall()

    by_name: dict[str, list] = defaultdict(list)
    by_file_name: dict[tuple, list] = defaultdict(list)
    for n in nodes:
        by_name[n["name"]].append(n)
        by_file_name[(n["file_id"], n["name"])].append(n)

    modules = {
        r["qname"]: r["id"]
        for r in conn.execute("SELECT id, qname FROM nodes WHERE kind = 'module'")
    }

    # what each file imports: local binding -> imported symbol name
    imports_by_file: dict[int, dict] = defaultdict(dict)
    for r in conn.execute("SELECT file_id, module, symbol, alias FROM imports"):
        local = r["alias"] or r["symbol"]
        if local and local != "*":
            imports_by_file[r["file_id"]][local] = r["symbol"] or local

    # class node id -> its own methods, for self. / this. receivers
    methods_of_class: dict[int, dict] = defaultdict(dict)
    for r in conn.execute(
        "SELECT e.src AS cls, n.name AS nm, n.id AS mid "
        "FROM edges e JOIN nodes n ON n.id = e.dst "
        "WHERE e.kind = 'contains' AND n.kind = 'method'"
    ):
        methods_of_class[r["cls"]][r["nm"]] = r["mid"]

    enclosing_class: dict[int, int] = {}
    for r in conn.execute(
        "SELECT e.dst AS child, e.src AS parent FROM edges e "
        "JOIN nodes p ON p.id = e.src "
        "WHERE e.kind = 'contains' AND p.kind = 'class'"
    ):
        enclosing_class[r["child"]] = r["parent"]

    counts = {"exact": 0, "heuristic": 0, "unresolved": 0, "extends": 0}

    pend = conn.execute(
        "SELECT p.id, p.src, p.callee, p.recv, p.file_id, p.line, f.lang AS lang "
        "FROM pending_calls p JOIN files f ON f.id = p.file_id"
    ).fetchall()

    for p in pend:
        found = _candidates(
            p, by_name, by_file_name, imports_by_file,
            methods_of_class, enclosing_class,
        )
        if not found:
            counts["unresolved"] += 1
            continue

        targets, resolution = found
        if len(targets) > MAX_AMBIGUOUS:
            counts["unresolved"] += 1
            continue

        kind = "extends" if p["recv"] == "__extends__" else "calls"
        for t in targets:
            db.insert_edge(
                conn, p["src"], t, kind,
                None if kind == "extends" else p["line"], resolution,
            )
        conn.execute("UPDATE pending_calls SET resolved = 1 WHERE id = ?", (p["id"],))
        counts["extends" if kind == "extends" else resolution] += 1

    # file-level import edges, for the module dependency view
    for r in conn.execute("SELECT file_id, module FROM imports"):
        src_mod = conn.execute(
            "SELECT id FROM nodes WHERE kind='module' AND file_id=?", (r["file_id"],)
        ).fetchone()
        if src_mod is None:
            continue
        target = _match_module(r["module"], modules)
        if target is not None and target != src_mod["id"]:
            db.insert_edge(conn, src_mod["id"], target, "imports")

    conn.commit()
    st = db.stats(conn)
    conn.close()
    st.update(counts)
    return st


def _candidates(p, by_name, by_file_name, imports_by_file,
                methods_of_class, enclosing_class):
    """Return (node_ids, resolution) or None."""
    name = p["callee"]
    recv = p["recv"]
    fid = p["file_id"]
    src = p["src"]

    # 1. defined in the same file
    local = by_file_name.get((fid, name))
    if local:
        return [local[0]["id"]], "exact"

    # 2. self.method() / this.method() inside a class
    if recv in ("self", "this"):
        cls = enclosing_class.get(src)
        if cls is not None and name in methods_of_class.get(cls, {}):
            return [methods_of_class[cls][name]], "exact"

    # 3. explicitly imported into this file
    imported = imports_by_file.get(fid, {})
    if name in imported:
        real = imported[name]
        pool = (by_name.get(real) or []) + (by_name.get(name) or [])
        hits = [h for h in pool if h["lang"] == p["lang"]]
        if hits:
            return [hits[0]["id"]], "exact"

    # 4 / 5. project-wide name match, within the SAME language.
    # Without the language filter a JS `import { load } from "./svc"` binds to a
    # Python `svc.load` purely because the names match, and is reported as an
    # exact call edge. Cross-language calls are not something static parsing can
    # see, so a same-name symbol in another language is a coincidence, not a
    # target.
    hits = [h for h in (by_name.get(name) or []) if h["lang"] == p["lang"]]
    if len(hits) == 1:
        return [hits[0]["id"]], "heuristic"
    if hits:
        return [h["id"] for h in hits], "heuristic"
    return None


def _match_module(spec: str, modules: dict):
    """Map an import specifier onto an indexed module qname, best effort."""
    cleaned = spec.replace("./", "").replace("../", "").replace("/", ".").lstrip(".")
    if cleaned in modules:
        return modules[cleaned]
    tail = cleaned.rsplit(".", 1)[-1]
    for q, nid in modules.items():
        if q == tail or q.endswith("." + tail):
            return nid
    return None
