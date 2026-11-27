"""Edits made between tool calls reach the graph before the next answer."""
from __future__ import annotations

import os
import textwrap
import time
from pathlib import Path

import pytest

from codeorbit import budget, db, freshness, mcp_server as ms
from codeorbit.indexer import index_project
from codeorbit.resolve import resolve_project


@pytest.fixture
def project(tmp_path) -> Path:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "mod.py").write_text(textwrap.dedent('''
        def parse_header(raw):
            return raw.split(":")
    '''), encoding="utf-8")
    index_project(tmp_path)
    resolve_project(tmp_path)
    freshness.forget()
    ms.LEDGER = budget.Ledger()
    return tmp_path


def _bump(p: Path) -> None:
    # Some filesystems keep coarse mtimes; make sure the edit is visible.
    t = time.time() + 5
    os.utime(p, (t, t))


def _node(root: Path, name: str) -> str:
    return ms.codeorbit_node(name, project_path=str(root))


def test_nothing_changed_means_no_reindex(project):
    assert freshness.refresh(project) == []
    assert freshness.refresh(project) == []


def test_an_edited_function_is_shown_as_it_is_now(project):
    first = _node(project, "parse_header")
    assert 'raw.split(":")' in first
    f = project / "pkg" / "mod.py"
    f.write_text("\n\n\ndef parse_header(raw):\n    return raw.partition('=')\n",
                 encoding="utf-8")
    _bump(f)
    second = _node(project, "parse_header")
    assert "raw.partition('=')" in second
    assert "already returned to you earlier" not in second    # it changed


def test_unchanged_files_still_cite_instead_of_resending(project):
    _node(project, "parse_header")
    _node(project, "parse_header")
    assert "already returned to you earlier" in _node(project, "parse_header")


def test_a_new_file_is_found(project):
    (project / "pkg" / "extra.py").write_text(
        "def brand_new_helper():\n    return 1\n", encoding="utf-8")
    _bump(project / "pkg")
    out = _node(project, "brand_new_helper")
    assert "def brand_new_helper" in out


def test_a_deleted_file_leaves_the_graph(project):
    (project / "pkg" / "mod.py").unlink()
    assert freshness.refresh(project) == ["pkg/mod.py"]
    conn = db.connect(project)
    try:
        assert conn.execute("SELECT count(*) FROM nodes WHERE name='parse_header'"
                            ).fetchone()[0] == 0
    finally:
        conn.close()
