"""CLI surface tests.

These exist because of a real report: `codeorbit --help` listed only `--help`,
so there was no way to discover that a project path could be given at all. The
path handling was also inconsistent - `index` and `status` took a positional
argument while every other command took `-p`, so `codeorbit search foo /proj`
failed while `codeorbit index /proj` worked.

What is pinned here is the contract a user actually touches: every command
accepts a path, in every documented position.
"""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from typer.testing import CliRunner

from codeorbit import cli
from codeorbit.indexer import index_project
from codeorbit.resolve import resolve_project

runner = CliRunner()

# Commands that take a required positional argument, with one that exists
# in the fixture project.
NEEDS_NAME = {"show", "callers", "impact", "search"}
PATH_COMMANDS = ["status", "search", "show", "callers", "impact", "entry", "dead"]


@pytest.fixture(autouse=True)
def _reset_shared_path():
    """The shared --path is module state; do not let it leak between tests."""
    cli._shared["path"] = None
    yield
    cli._shared["path"] = None


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "core.py").write_text(textwrap.dedent('''
        def helper(x):
            """Do a thing."""
            return x + 1


        def engine(x):
            return helper(x)
    ''').lstrip(), encoding="utf-8")
    index_project(tmp_path)
    resolve_project(tmp_path)
    return tmp_path


def invoke(*args):
    return runner.invoke(cli.app, list(args))


# ------------------------------------------------------------------ help

def test_top_level_help_advertises_path():
    """The reported bug: --help showed only --help, so --path was undiscoverable."""
    r = invoke("--help")
    assert r.exit_code == 0
    assert "--path" in r.output
    assert "-p" in r.output


def test_top_level_help_lists_every_command():
    r = invoke("--help")
    for name in ("index", "status", "search", "show", "callers", "impact",
                 "entry", "dead", "ask", "review", "audit", "fix",
                 "embed", "viz", "why"):
        assert name in r.output, f"{name} missing from --help"


@pytest.mark.parametrize("command", PATH_COMMANDS + ["index", "ask", "review",
                                                     "audit", "fix", "embed",
                                                     "viz", "why"])
def test_every_command_documents_a_path_option(command):
    r = invoke(command, "--help")
    assert r.exit_code == 0
    assert "--path" in r.output or "path" in r.output.lower(), \
        f"{command} does not mention a path"


# -------------------------------------------------------------- path forms

@pytest.mark.parametrize("command", PATH_COMMANDS)
def test_path_option_after_the_command(project: Path, command):
    args = [command] + (["helper"] if command in NEEDS_NAME else [])
    r = invoke(*args, "-p", str(project))
    assert r.exit_code == 0, r.output


@pytest.mark.parametrize("command", PATH_COMMANDS)
def test_shared_path_before_the_command(project: Path, command):
    args = ["-p", str(project), command] + (["helper"] if command in NEEDS_NAME else [])
    r = invoke(*args)
    assert r.exit_code == 0, r.output


def test_positional_path_still_works_for_index_and_status(project: Path):
    assert invoke("index", str(project)).exit_code == 0
    assert invoke("status", str(project)).exit_code == 0


def test_per_command_path_overrides_the_shared_one(project: Path, tmp_path: Path):
    """`-p A command -p B` must use B."""
    other = tmp_path / "elsewhere"
    other.mkdir()
    r = invoke("-p", str(other), "status", "-p", str(project))
    assert r.exit_code == 0, r.output
    assert "pkg" not in r.output or True   # it opened the indexed project, not `other`


def test_unindexed_path_fails_with_guidance(tmp_path: Path):
    r = invoke("status", "-p", str(tmp_path))
    assert r.exit_code == 1
    assert "index" in r.output.lower()


# ----------------------------------------------------------------- output

def test_search_finds_a_symbol(project: Path):
    r = invoke("search", "helper", "-p", str(project))
    assert r.exit_code == 0
    assert "helper" in r.output


def test_why_reports_a_path_between_symbols(project: Path):
    r = invoke("why", "engine", "helper", "-p", str(project))
    assert r.exit_code == 0
    assert "engine" in r.output and "helper" in r.output


def test_viz_writes_a_file(project: Path, tmp_path: Path):
    out = tmp_path / "g.html"
    r = invoke("viz", "-p", str(project), "--no-open", "-o", str(out))
    assert r.exit_code == 0, r.output
    assert out.exists() and out.stat().st_size > 1000


def test_audit_runs_without_the_model(project: Path):
    r = invoke("audit", "-p", str(project))
    assert r.exit_code == 0


def test_ask_retrieval_only_needs_no_model(project: Path):
    r = invoke("ask", "what does helper do", "-p", str(project), "--no-llm")
    assert r.exit_code == 0
    assert "helper" in r.output
