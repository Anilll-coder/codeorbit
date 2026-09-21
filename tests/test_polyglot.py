"""The generic extractor, against one real file per language.

These are the tests that say the expansion is real rather than a list of
suffixes. For each language: does a class come out as a class, a method as a
method under its type, a call as a call, and an import as an import. If the
generic walker regresses on any of them the language silently degrades to a
module node with nothing in it, which looks exactly like an empty file.

A second set covers what the *inference* path does for a language with no table
entry, because that is the part carrying the long tail.
"""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from codeorbit import db, langs, query
from codeorbit.extract import parse_and_extract
from codeorbit.indexer import index_project
from codeorbit.resolve import resolve_project

pytestmark = pytest.mark.skipif(
    langs._pack() is None,
    reason="tree-sitter-language-pack is not installed",
)


def ex(source: str, lang: str, path: str):
    return parse_and_extract(textwrap.dedent(source).lstrip().encode(), lang, path)


def kinds(extraction) -> dict[str, str]:
    return {s.qname: s.kind for s in extraction.symbols}


def callees(extraction) -> set[str]:
    return {c.callee for c in extraction.calls}


def modules(extraction) -> set[str]:
    return {i.module for i in extraction.imports}


# ---------------------------------------------------------------- per-language

CASES = {
    "go": ("svc.go", '''
        package main
        import "fmt"
        type Server struct{ port int }
        func (s *Server) Start() error { return listen(s.port) }
        func listen(p int) error { fmt.Println(p); return nil }
    ''', {"svc.Server": "class", "svc.Server.Start": "method",
          "svc.listen": "function"}, {"listen", "Println"}, {"fmt"}),

    "rust": ("lib.rs", '''
        use std::fmt;
        pub struct Engine { n: u32 }
        impl Engine { pub fn run(&self) -> u32 { helper(self.n) } }
        fn helper(x: u32) -> u32 { x + 1 }
    ''', {"lib.Engine": "class", "lib.Engine.run": "method",
          "lib.helper": "function"}, {"helper"}, {"std::fmt"}),

    "java": ("Svc.java", '''
        import java.util.List;
        public class Service extends Base {
            public void run() { helper(); }
            private void helper() {}
        }
    ''', {"Svc.Service": "class", "Svc.Service.run": "method",
          "Svc.Service.helper": "method"}, {"helper"}, {"java.util.List"}),

    "ruby": ("job.rb", '''
        require "json"
        class Worker < Base
          def perform
            helper
          end
        end
        def helper; end
    ''', {"job.Worker": "class", "job.Worker.perform": "method",
          "job.helper": "function"}, set(), {"json"}),

    "php": ("h.php", '''
        <?php
        use App\\Base;
        class Handler extends Base {
            public function handle() { return $this->helper(); }
            private function helper() { return 1; }
        }
    ''', {"h.Handler": "class", "h.Handler.handle": "method",
          "h.Handler.helper": "method"}, {"helper"}, None),

    "csharp": ("R.cs", '''
        using System;
        namespace App {
            public class Runner : Base {
                public void Run() { Helper(); }
                private void Helper() {}
            }
        }
    ''', {"R.App.Runner": "class", "R.App.Runner.Run": "method",
          "R.App.Runner.Helper": "method"}, {"Helper"}, {"System"}),

    "kotlin": ("C.kt", '''
        import kotlin.math.abs
        class Calc : Base() {
            fun run(x: Int): Int = helper(abs(x))
            private fun helper(x: Int) = x + 1
        }
    ''', {"C.Calc": "class", "C.Calc.run": "method", "C.Calc.helper": "method"},
        {"helper", "abs"}, {"kotlin.math.abs"}),

    "elixir": ("p.ex", '''
        defmodule Pipeline do
          import Logger
          def run(x) do
            helper(x)
          end
          defp helper(x), do: x * 2
        end
    ''', {"p.Pipeline": "class", "p.Pipeline.run": "method",
          "p.Pipeline.helper": "method"}, {"helper"}, {"Logger"}),

    "c": ("m.c", '''
        #include <stdio.h>
        static int helper(int a) { return a + 1; }
        int main(void) { return helper(1); }
    ''', {"m.helper": "function", "m.main": "function"}, {"helper"},
        {"stdio.h"}),

    "swift": ("S.swift", '''
        import Foundation
        class Store: NSObject {
            func load() -> Int { return helper() }
            func helper() -> Int { return 1 }
        }
    ''', {"S.Store": "class", "S.Store.load": "method",
          "S.Store.helper": "method"}, {"helper"}, {"Foundation"}),

    "bash": ("d.sh", '''
        helper() { echo "$1"; }
        run() { helper "hello"; }
    ''', {"d.helper": "function", "d.run": "function"}, {"helper", "echo"},
        None),

    "solidity": ("V.sol", '''
        import "./Base.sol";
        contract Vault is Base {
            function deposit() public { _credit(); }
            function _credit() internal {}
        }
    ''', {"V.Vault": "class", "V.Vault.deposit": "method",
          "V.Vault._credit": "method"}, {"_credit"}, {"./Base.sol"}),
}


@pytest.mark.parametrize("lang", sorted(CASES))
def test_symbols_come_out_with_the_right_kind_and_scope(lang):
    path, source, expected, _, _ = CASES[lang]
    got = kinds(ex(source, lang, path))
    for qname, kind in expected.items():
        assert got.get(qname) == kind, f"{lang}: expected {kind} {qname}, got {got}"


@pytest.mark.parametrize("lang", sorted(CASES))
def test_calls_are_found(lang):
    path, source, _, expected, _ = CASES[lang]
    if not expected:
        pytest.skip("no call assertion for this language")
    assert expected <= callees(ex(source, lang, path))


@pytest.mark.parametrize("lang", sorted(CASES))
def test_imports_are_found(lang):
    path, source, _, _, expected = CASES[lang]
    if expected is None:
        pytest.skip("import shape is not asserted for this language")
    assert expected <= modules(ex(source, lang, path))


# --------------------------------------------------------- language details

def test_a_go_method_belongs_to_its_receiver_type():
    """Go declares methods at top level. Without reading the receiver, every
    method in the package lands beside the functions and two types with a
    `Close` collide."""
    out = kinds(ex('''
        package db
        type Conn struct{}
        func (c *Conn) Close() error { return nil }
    ''', "go", "db.go"))
    assert out["db.Conn.Close"] == "method"


def test_a_rust_impl_does_not_duplicate_its_struct():
    """`struct Engine` and `impl Engine` are one type. Emitting a node for
    each splits it in two and halves every fan-in count on it."""
    out = kinds(ex('''
        pub struct Engine;
        impl Engine { pub fn run(&self) {} }
    ''', "rust", "e.rs"))
    assert [q for q, k in out.items() if k == "class"] == ["e.Engine"]
    assert out["e.Engine.run"] == "method"


def test_base_types_are_recorded_across_languages():
    for lang, path, source, base in [
        ("java", "A.java", "class Service extends Base {}", "Base"),
        ("php", "a.php", "<?php class H extends Base {}", "Base"),
        ("kotlin", "A.kt", "class Calc : Base()", "Base"),
        ("ruby", "a.rb", "class Worker < Base\nend\n", "Base"),
    ]:
        bases = {b for _, b in ex(source, lang, path).base_types}
        assert base in bases, f"{lang}: no base type recorded"


def test_an_untabled_language_yields_a_full_graph():
    """The inference path, which carries the ~350 languages nobody has written
    a table entry for. Gleam has no entry and needs none: its node types are
    spelled `function`, `function_call` and `import`."""
    if "gleam" not in langs.supported():
        pytest.skip("gleam grammar unavailable")
    out = ex('''
        import gleam/io
        pub fn helper(x) { x + 1 }
        pub fn run(x) { helper(x) }
    ''', "gleam", "m.gleam")
    assert {"m.helper", "m.run"} <= set(kinds(out))
    assert "helper" in callees(out)
    assert "gleam/io" in modules(out)


@pytest.mark.parametrize("lang,path,source", [
    ("nim", "m.nim", "proc helper(x: int): int = x + 1\n"
                     "proc run(x: int): int = helper(x)\n"),
    ("crystal", "m.cr", "class A\n  def helper\n    1\n  end\nend\n"),
])
def test_inference_finds_definitions_in_unusual_grammars(lang, path, source):
    """Nim spells a function `routine` and Crystal spells one `method_def`.
    Neither has a table entry, and neither should index to nothing.

    Deliberately only about definitions: these grammars name their call nodes
    in ways the patterns do not reach, so the graph for them is symbols and
    imports without call edges. That is a real limit and worth pinning, because
    the alternative is inventing edges that are not there.
    """
    names = {s.name for s in ex(source, lang, path).symbols}
    assert "helper" in names


# ---------------------------------------------------------------- embedded

def test_a_vue_component_yields_its_script():
    out = ex('''
        <template><div>{{ n }}</div></template>
        <script setup lang="ts">
        import { ref } from 'vue'
        function bump(): void { increment(1) }
        function increment(x: number): number { return x + 1 }
        </script>
    ''', "vue", "Card.vue")
    assert {"Card.bump", "Card.increment"} <= set(kinds(out))
    assert "increment" in callees(out)
    assert "vue" in modules(out)


def test_embedded_line_numbers_point_at_the_real_line():
    """A citation that is off by the height of the template is worse than no
    citation: the reader follows it and lands on markup."""
    source = textwrap.dedent('''
        <template>
          <div />
        </template>

        <script>
        function bump() { return 1 }
        </script>
    ''').lstrip()
    out = ex(source, "vue", "C.vue")
    bump = next(s for s in out.symbols if s.name == "bump")
    assert source.splitlines()[bump.start_line - 1].strip().startswith("function bump")


def test_markup_with_no_script_is_a_module_not_a_failure():
    out = ex("<template><div>hi</div></template>", "vue", "Static.vue")
    assert [s.kind for s in out.symbols] == ["module"]


# ------------------------------------------------------------- end to end

def test_a_polyglot_repo_indexes_and_resolves(tmp_path: Path):
    """Two languages in one repo, each resolving inside itself and neither
    binding to the other."""
    (tmp_path / "a.go").write_text(
        'package main\nfunc helper() int { return 1 }\n'
        'func main() { helper() }\n', encoding="utf-8")
    (tmp_path / "b.py").write_text(
        'def helper():\n    return 2\n\n\ndef main():\n    return helper()\n',
        encoding="utf-8")

    index_project(tmp_path)
    resolve_project(tmp_path)
    conn = db.connect(tmp_path)
    try:
        langs_seen = {r["lang"] for r in conn.execute("SELECT lang FROM files")}
        assert langs_seen == {"go", "python"}

        go_helper = next(r for r in query.search(conn, "helper")
                         if r["lang"] == "go" and r["kind"] == "function")
        callers = query.callers(conn, go_helper["id"])
        assert callers, "the Go call should resolve"
        assert all(c["lang"] == "go" for c in callers), \
            "a Python caller here would be a coincidence, not a call"
    finally:
        conn.close()
