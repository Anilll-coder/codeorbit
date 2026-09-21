"""The language registry: what CodeOrbit will read, and what it admits it won't.

The registry is the one place that decides whether a repository indexes at all,
so the tests that matter are about honesty. A suffix that maps to a grammar
nobody has installed must not be scanned - a file row with no symbols reads as
"this file is empty", which is a lie the graph then repeats to the model.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from codeorbit import langs
from codeorbit.scanner import lang_of, scan, unsupported

pack = pytest.mark.skipif(
    langs._pack() is None,
    reason="tree-sitter-language-pack is not installed",
)


# ------------------------------------------------------------------ lookup

@pytest.mark.parametrize("name,expected", [
    ("main.py", "python"), ("app.tsx", "tsx"), ("server.go", "go"),
    ("lib.rs", "rust"), ("App.java", "java"), ("view.rb", "ruby"),
    ("index.php", "php"), ("Main.kt", "kotlin"), ("app.swift", "swift"),
    ("m.ex", "elixir"), ("q.sql", "sql"), ("deploy.sh", "bash"),
    ("Card.vue", "vue"), ("Nav.svelte", "svelte"), ("main.dart", "dart"),
])
def test_common_files_map_to_a_language(name, expected):
    assert langs.lang_for(name) == expected


@pytest.mark.parametrize("name,expected", [
    ("Dockerfile", "dockerfile"), ("Makefile", "make"),
    ("Gemfile", "ruby"), ("CMakeLists.txt", "cmake"),
])
def test_files_named_rather_than_suffixed(name, expected):
    """Half the build system in any repo has no extension at all."""
    assert langs.lang_for(name) == expected


def test_a_double_suffix_beats_its_tail():
    """`page.blade.php` is Blade. Matching `.php` first would parse the
    template's PHP and miss everything the directives do."""
    assert langs.lang_for("page.blade.php") == "blade"
    assert langs.lang_for("plain.php") == "php"


def test_an_unknown_extension_is_not_guessed():
    assert langs.lang_for("notes.txt") is None
    assert langs.lang_for("image.png") is None


# ---------------------------------------------------------------- families

def test_typescript_and_javascript_share_a_family():
    """They compile together and call each other, so name resolution must
    cross between them."""
    fam = langs.family
    assert fam("typescript") == fam("javascript") == fam("tsx") == fam("vue")


def test_unrelated_languages_do_not_share_a_family():
    """A Go `Load` and a Python `load` are not the same function, and binding
    them produced call edges that were pure coincidence."""
    assert langs.family("go") != langs.family("python")
    assert langs.family("rust") != langs.family("javascript")


def test_an_unregistered_language_is_its_own_family():
    assert langs.family("cobol") == "cobol"


# --------------------------------------------------------------- support

def test_the_three_builtin_grammars_are_always_available():
    """These are direct dependencies. They must work with no pack installed."""
    for lang in ("python", "javascript", "typescript", "tsx"):
        assert langs.is_supported(lang)
        assert langs.parser(lang) is not None


def test_supported_is_derived_not_declared():
    """Every language claimed as supported must actually load a grammar,
    or `scan` will index files it cannot parse."""
    for lang in sorted(langs.supported()):
        assert langs.grammar(lang) is not None, f"{lang} claims support but has no grammar"


def test_nothing_unloadable_is_claimed():
    assert langs.grammar("a-language-that-does-not-exist") is None


@pack
def test_the_pack_widens_coverage_a_lot():
    """The whole point of the change: three languages is the wrong answer for
    most repositories."""
    assert len(langs.supported()) > 50
    for lang in ("go", "rust", "java", "ruby", "php", "csharp", "kotlin"):
        assert lang in langs.supported()


# ---------------------------------------------------------------- scanning

def test_generated_files_are_skipped(tmp_path: Path):
    for name in ("app.min.js", "types.d.ts", "api_pb2.py", "model.g.dart"):
        (tmp_path / name).write_text("x = 1\n", encoding="utf-8")
    assert lang_of(tmp_path / "app.min.js") is None
    assert scan(tmp_path) == []


def test_build_output_directories_are_skipped(tmp_path: Path):
    (tmp_path / ".gradle").mkdir()
    (tmp_path / ".gradle" / "cached.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "real.py").write_text("x = 1\n", encoding="utf-8")
    found = {p.name for p, _ in scan(tmp_path)}
    assert found == {"real.py"}


def test_a_file_with_no_grammar_is_skipped_not_indexed_empty(tmp_path: Path,
                                                             monkeypatch):
    """A file row with no symbols is indistinguishable from an empty file."""
    monkeypatch.setattr(langs, "supported", lambda: frozenset({"python"}))
    (tmp_path / "a.py").write_text("def f(): pass\n", encoding="utf-8")
    (tmp_path / "b.go").write_text("package main\n", encoding="utf-8")
    assert {p.name for p, _ in scan(tmp_path)} == {"a.py"}


def test_unsupported_reports_what_was_left_out(tmp_path: Path, monkeypatch):
    """An index that produced nothing has to say why, or the obvious
    conclusion is that the tool is broken."""
    monkeypatch.setattr(langs, "supported", lambda: frozenset({"python"}))
    (tmp_path / "a.go").write_text("package main\n", encoding="utf-8")
    (tmp_path / "b.go").write_text("package main\n", encoding="utf-8")
    (tmp_path / "c.rb").write_text("puts 1\n", encoding="utf-8")
    assert dict(unsupported(tmp_path)) == {"go": 2, "ruby": 1}
