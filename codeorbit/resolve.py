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
from .langs import family

MAX_AMBIGUOUS = 3

# Every way a language spells "a method on the type I am inside".
SELF_RECEIVERS = frozenset({
    "self", "this", "$this", "Self", "me", "cls", "&self", "&mut self",
    "super", "parent", "@", "it",
})


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

    # file-level import edges, for the module dependency view.
    # Indexed by every suffix of each module qname, so matching an import
    # specifier against them is a dict lookup rather than a scan of every
    # module per import - which on a large repo was the slowest part of resolve.
    by_tail: dict[str, list[int]] = defaultdict(list)
    for qname, nid in modules.items():
        parts = qname.split(".")
        for start in range(len(parts)):
            by_tail[".".join(parts[start:])].append(nid)

    mod_of_file = {
        r["file_id"]: r["id"]
        for r in conn.execute("SELECT id, file_id FROM nodes WHERE kind='module'")
    }
    for r in conn.execute("SELECT file_id, module FROM imports"):
        src_mod_id = mod_of_file.get(r["file_id"])
        if src_mod_id is None:
            continue
        src_mod = {"id": src_mod_id}
        target = _match_module(r["module"], modules, by_tail)
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

    # 2. self.method() / this.method() inside a class.
    # One list, because every language spells the same idea differently and a
    # Ruby `self.`, a PHP `$this->`, a Rust `Self::` and a Go receiver name all
    # mean "a method on the type I am inside".
    if recv in SELF_RECEIVERS:
        cls = enclosing_class.get(src)
        if cls is not None and name in methods_of_class.get(cls, {}):
            return [methods_of_class[cls][name]], "exact"

    # 3. explicitly imported into this file
    imported = imports_by_file.get(fid, {})
    if name in imported:
        real = imported[name]
        pool = (by_name.get(real) or []) + (by_name.get(name) or [])
        fam = family(p["lang"])
        hits = [h for h in pool if family(h["lang"]) == fam]
        if hits:
            return [hits[0]["id"]], "exact"

    # 4 / 5. project-wide name match, within the same language FAMILY.
    # Without the filter a JS `import { load } from "./svc"` binds to a Python
    # `svc.load` purely because the names match, and is reported as an exact
    # call edge. Cross-language calls are not something static parsing can see,
    # so a same-name symbol in another language is a coincidence, not a target.
    #
    # A family rather than an exact match, because the languages that really do
    # call each other are the ones that compile together: a .ts module importing
    # a .js one, a .vue component calling a .ts helper, a .mm file calling C.
    # Requiring `lang == lang` there dropped every edge across those pairs.
    fam = family(p["lang"])
    hits = [h for h in (by_name.get(name) or []) if family(h["lang"]) == fam]
    if len(hits) == 1:
        return [hits[0]["id"]], "heuristic"
    if hits:
        return [h["id"] for h in hits], "heuristic"
    return None


# Separators that mean "namespace" in some language: Rust and C++ `::`,
# PHP and Java inner classes `\`, everything else `/` or `.`.
_SEPARATORS = ("::", "\\", "/", ":")

# Suffixes an import specifier carries that the module qname does not.
_IMPORT_SUFFIXES = (".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".vue",
                    ".svelte", ".astro", ".py", ".rb", ".php", ".go", ".rs",
                    ".h", ".hpp", ".sol", ".dart", ".sh", ".lua", ".ex")


def _normalise_spec(spec: str) -> str:
    """An import specifier reduced to the dotted shape module qnames use.

    Import syntax is the least portable thing in any language. The same
    dependency is written `from pkg.core import x`, `require("./pkg/core")`,
    `import "example.com/app/pkg/core"`, `use crate::pkg::core`,
    `#include "pkg/core.h"` and `import app.pkg.core.Core`. All of them name
    the same file, and the module view is wrong - shows a project of isolated
    islands - unless they all reduce to the same thing.
    """
    spec = spec.strip().strip("\"'<>`;")
    for sep in _SEPARATORS:
        spec = spec.replace(sep, ".")
    spec = spec.replace("./", "").replace("../", "").replace("/", ".")
    for suffix in _IMPORT_SUFFIXES:
        if spec.endswith(suffix):
            spec = spec[: -len(suffix)]
            break
    # `crate`, `self` and `super` are Rust's way of saying "here"; a Go import
    # path carries a whole domain in front of the part that is in this repo.
    parts = [p for p in spec.split(".") if p and p not in ("crate", "self", "super")]
    return ".".join(parts).strip(".")


def _match_module(spec: str, modules: dict, by_tail: dict):
    """Map an import specifier onto an indexed module qname, best effort."""
    cleaned = _normalise_spec(spec)
    if not cleaned:
        return None
    if cleaned in modules:
        return modules[cleaned]

    # A longest-suffix match: `example.com/app/pkg/core` should find `pkg.core`
    # rather than any module whose last segment happens to be `core`.
    parts = cleaned.split(".")
    for start in range(len(parts)):
        candidate = ".".join(parts[start:])
        if candidate in modules:
            return modules[candidate]
        hit = by_tail.get(candidate)
        if hit is not None and len(hit) == 1:
            return hit[0]

    # Java and PHP import a class, not a module: the last segment is the type
    # and the module is everything before it.
    if len(parts) > 1:
        head = ".".join(parts[:-1])
        if head in modules:
            return modules[head]
        hit = by_tail.get(head)
        if hit is not None and len(hit) == 1:
            return hit[0]

    # Last resort, and only when it is unambiguous. Binding `core` to whichever
    # of five modules ends in `.core` is how the module view fills with edges
    # nobody can check.
    tail = parts[-1]
    hit = by_tail.get(tail)
    return hit[0] if hit is not None and len(hit) == 1 else None
