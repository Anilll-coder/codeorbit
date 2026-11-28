"""The lean MCP profile: few, small tool definitions and grep-sized answers."""
from __future__ import annotations

import asyncio
import json
import textwrap
from pathlib import Path

import pytest

from codeorbit import budget, db, mcp_lean as ml, mcp_server as ms
from codeorbit.indexer import index_project
from codeorbit.resolve import resolve_project


@pytest.fixture(scope="module")
def project(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("lean")
    (root / "pkg").mkdir()
    filler = "\n".join(f"    x{i} = {i}" for i in range(60))
    (root / "pkg" / "proxy.py").write_text(textwrap.dedent('''
        def strip_proxy_header(request,
                               keep=False):
    ''') + filler + "\n    request.proxy_header = None\n    return request\n\n\n"
        "def send(request):\n    return strip_proxy_header(request)\n", encoding="utf-8")
    index_project(root)
    resolve_project(root)
    return root


def test_definitions_are_small():
    tools = asyncio.run(ml.lean.list_tools())
    assert [t.name for t in tools] == ["codeorbit_find", "codeorbit_show"]
    size = sum(len(json.dumps({"n": t.name, "d": t.description, "s": t.input_schema}))
               for t in tools) + len(ml.INSTRUCTIONS)
    assert size // 4 < 300                       # the full profile is ~800 tokens


def test_find_is_grep_sized_and_points_at_the_lines(project):
    ms.LEDGER = budget.Ledger()
    conn = db.connect(project)
    try:
        out = ml.find_text(project, conn, "proxy header is not stripped")
    finally:
        conn.close()
    assert out.splitlines()[0] == "pkg/proxy.py"
    assert "function strip_proxy_header(request, keep=False)" in out
    assert "request.proxy_header = None" in out    # the matching line, numbered
    assert "keep=False):" not in out.split("\n", 2)[2]   # the signature is not repeated
    excerpt = [l for l in out.splitlines() if " | " in l]
    assert 0 < len(excerpt) <= ml.EXCERPT_LINES     # the best match: a capped excerpt
    assert len(out) // 4 < 400                       # explore's first answer: ~2,400


def test_asking_again_lists_new_places(project):
    ms.LEDGER = budget.Ledger()
    conn = db.connect(project)
    try:
        a = ml.find_text(project, conn, "proxy header")
        b = ml.find_text(project, conn, "proxy header")
    finally:
        conn.close()
    assert "function strip_proxy_header" in a
    assert "function strip_proxy_header" not in b      # named as already listed, at most
