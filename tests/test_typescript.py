"""TypeScript / TSX extraction.

TypeScript reuses the JavaScript walker, so these tests cover two things: that
the shared path still behaves for TS input (a method must land on its class,
not at module scope), and that the TS-only declarations - interface, type, enum,
implements - become symbols and edges rather than being silently skipped.
"""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from codeorbit import db, query
from codeorbit.extract import parse_and_extract
from codeorbit.indexer import index_project
from codeorbit.resolve import resolve_project
from codeorbit.scanner import LANG_BY_SUFFIX, scan


def ex(src: str, lang: str = "typescript", path: str = "src/m.ts"):
    return parse_and_extract(textwrap.dedent(src).lstrip().encode(), lang, path)


def kinds(extraction) -> dict:
    return {s.qname: s.kind for s in extraction.symbols}


# ------------------------------------------------------------------ wiring

@pytest.mark.parametrize("suffix,lang", [
    (".ts", "typescript"), (".mts", "typescript"), (".cts", "typescript"),
    (".tsx", "tsx"),
])
def test_suffixes_are_recognised(suffix, lang):
    assert LANG_BY_SUFFIX[suffix] == lang


def test_declaration_files_are_not_indexed(tmp_path: Path):
    """A .d.ts has no bodies and no call sites - nothing a graph can use."""
    (tmp_path / "types.d.ts").write_text("export declare function f(): void;\n",
                                         encoding="utf-8")
    (tmp_path / "real.ts").write_text("export function g() { return 1; }\n",
                                      encoding="utf-8")
    found = {p.name for p, _ in scan(tmp_path)}
    assert "real.ts" in found
    assert "types.d.ts" not in found


# ------------------------------------------------------------- TS-only kinds

def test_interface_and_its_members():
    e = ex("""
        export interface Repo {
          find(id: string): User;
          readonly name: string;
        }
    """)
    k = kinds(e)
    assert k["src.m.Repo"] == "interface"
    assert k["src.m.Repo.find"] == "method"
    assert k["src.m.Repo.name"] == "property"


def test_type_alias_is_a_symbol():
    assert kinds(ex("export type Id = string | number;"))["src.m.Id"] == "type_alias"


def test_enum_and_members():
    k = kinds(ex("export enum Kind { Draft, Published }"))
    assert k["src.m.Kind"] == "enum"
    assert k["src.m.Kind.Draft"] == "enum_member"
    assert k["src.m.Kind.Published"] == "enum_member"


# ---------------------------------------------------- the shared JS walker

def test_a_method_on_an_abstract_class_is_scoped_to_it():
    """Regression: the JS walker did not know abstract_class_declaration, so it
    descended into the class as an unknown node and every method landed at
    module scope."""
    k = kinds(ex("""
        export abstract class Base {
          find(id: string) { return 1; }
        }
    """))
    assert k["src.m.Base"] == "class"
    assert "src.m.Base.find" in k, "the method must belong to its class"
    assert "src.m.find" not in k, "it must not also appear at module scope"


def test_arrow_functions_and_calls_still_work():
    e = ex("""
        export const helper = async (x: number): Promise<void> => { await save(x); };
        function plain(a: Id) { return helper(1); }
    """)
    k = kinds(e)
    assert k["src.m.helper"] == "function"
    assert k["src.m.plain"] == "function"
    assert {"save", "helper"} <= {c.callee for c in e.calls}


def test_typed_imports_are_captured():
    e = ex('import { db, type User } from "./db";')
    assert ("./db", "db") in [(i.module, i.symbol) for i in e.imports]


# ------------------------------------------------------------- inheritance

def test_implements_is_recorded():
    e = ex("""
        interface Repo { find(): void; }
        export class Impl implements Repo { find() { return 1; } }
    """)
    assert ("src.m.Impl", "Repo") in e.base_types


def test_extends_and_implements_together_are_separate_edges():
    """Regression: stripping only the word "extends" from the heritage text
    recorded "Base implements Repo" as one base name."""
    e = ex("export class Impl extends Base implements Repo { m() { return 1; } }")
    bases = dict.fromkeys(b for a, b in e.base_types)
    assert "Base" in bases and "Repo" in bases
    assert not any("implements" in b for _, b in e.base_types)


def test_no_duplicate_inheritance_edges():
    e = ex("export class A extends B implements C {}")
    assert len(e.base_types) == len(set(e.base_types))


def test_interface_extending_an_interface():
    e = ex("export interface Admin extends User { level: number; }")
    assert ("src.m.Admin", "User") in e.base_types


# -------------------------------------------------------------------- TSX

def test_tsx_parses_jsx_and_types_together():
    e = ex("""
        interface Props { title: string; }
        export function Card({ title }: Props) {
          return <div onClick={() => handle(title)}>{title}</div>;
        }
    """, lang="tsx", path="src/Card.tsx")
    k = kinds(e)
    assert k["src.Card.Props"] == "interface"
    assert k["src.Card.Card"] == "function"
    assert "handle" in {c.callee for c in e.calls}


def test_ts_grammar_would_fail_on_jsx_so_tsx_is_separate():
    """.tsx must use the tsx grammar; the plain TS one misreads JSX."""
    plain = ex("export function C() { return <div>{x()}</div>; }",
               lang="tsx", path="c.tsx")
    assert "c.C" in kinds(plain)


# ------------------------------------------------------------ end to end

def test_typescript_project_indexes_and_resolves(tmp_path: Path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "db.ts").write_text(textwrap.dedent("""
        export function lookup(id: string) { return { id }; }
    """).lstrip(), encoding="utf-8")
    (tmp_path / "src" / "repo.ts").write_text(textwrap.dedent("""
        import { lookup } from "./db";

        export interface Repo { find(id: string): object; }

        export class Impl implements Repo {
          find(id: string) { return lookup(id); }
        }
    """).lstrip(), encoding="utf-8")

    index_project(tmp_path)
    resolve_project(tmp_path)
    conn = db.connect(tmp_path)
    try:
        assert query.search(conn, "Repo"), "the interface should be searchable"
        lookup = next(r for r in query.search(conn, "lookup")
                      if r["kind"] == "function")
        callers = query.callers(conn, lookup["id"])
        assert callers, "Impl.find calls lookup across files"

        n = conn.execute(
            "SELECT count(*) FROM edges WHERE kind = 'extends'").fetchone()[0]
        assert n >= 1, "implements should produce an inheritance edge"
    finally:
        conn.close()
