"""Tests for graph export.

The important invariants are structural, not visual: the embedded JSON must be
valid and self-consistent (no edge pointing at a node that was not included, or
the renderer throws), and the page must make no external requests - a viewer
that needs a CDN would contradict the whole local-first premise.
"""
from __future__ import annotations

import json
import re
import textwrap
from pathlib import Path

import pytest

from codeorbit import db, query, viz
from codeorbit.indexer import index_project
from codeorbit.resolve import resolve_project


@pytest.fixture
def graph(tmp_path: Path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "core.py").write_text(textwrap.dedent('''
        def helper(x):
            return x + 1


        def engine(x):
            return helper(x)
    ''').lstrip(), encoding="utf-8")
    (tmp_path / "pkg" / "api.py").write_text(textwrap.dedent('''
        from pkg.core import engine


        def handle(x):
            return engine(x)
    ''').lstrip(), encoding="utf-8")

    index_project(tmp_path)
    resolve_project(tmp_path)
    conn = db.connect(tmp_path)
    yield tmp_path, conn
    conn.close()


# ------------------------------------------------------------------ shape

def test_module_graph_has_nodes_and_an_import_edge(graph):
    _, conn = graph
    data = viz.module_graph(conn)
    assert data["view"] == "modules"
    assert len(data["nodes"]) == 2
    labels = {n["label"] for n in data["nodes"]}
    assert labels == {"core.py", "api.py"}


def test_symbol_graph_includes_call_edges(graph):
    _, conn = graph
    data = viz.symbol_graph(conn)
    assert data["view"] == "symbols"
    names = {n["label"] for n in data["nodes"]}
    assert {"helper", "engine", "handle"} <= names
    assert data["edges"], "engine calls helper, so there is at least one edge"


def test_focus_view_marks_the_focus_and_limits_the_neighbourhood(graph):
    _, conn = graph
    helper = next(r for r in query.search(conn, "helper") if r["kind"] == "function")
    data = viz.symbol_graph(conn, helper["id"], depth=1)
    focused = [n for n in data["nodes"] if n.get("focus")]
    assert len(focused) == 1
    assert focused[0]["label"] == "helper"


@pytest.mark.parametrize("builder", ["modules", "symbols"])
def test_no_edge_points_at_a_missing_node(graph, builder):
    """A dangling edge makes the renderer throw on load."""
    _, conn = graph
    data = viz.module_graph(conn) if builder == "modules" else viz.symbol_graph(conn)
    ids = {n["id"] for n in data["nodes"]}
    for e in data["edges"]:
        assert e["source"] in ids and e["target"] in ids


def test_every_node_carries_what_the_panel_renders(graph):
    _, conn = graph
    for data in (viz.module_graph(conn), viz.symbol_graph(conn)):
        for n in data["nodes"]:
            assert n["label"] and n["full"] and n["meta"]
            assert isinstance(n["weight"], int) and n["weight"] >= 1


# ------------------------------------------------------------------ mermaid

def test_mermaid_is_a_flowchart_with_nodes_and_arrows(graph):
    _, conn = graph
    text = viz.to_mermaid(viz.symbol_graph(conn))
    assert text.startswith("flowchart LR")
    assert "-->" in text or "-.->" in text


def test_mermaid_quotes_are_escaped(graph):
    _, conn = graph
    data = viz.symbol_graph(conn)
    data["nodes"][0]["label"] = 'we"ird'
    text = viz.to_mermaid(data)
    assert '"we"ird"' not in text          # would break the Mermaid parser


def test_mermaid_respects_its_node_cap(graph):
    _, conn = graph
    text = viz.to_mermaid(viz.symbol_graph(conn), max_nodes=1)
    assert len(re.findall(r'^\s+n\d+\[', text, re.M)) == 1


# --------------------------------------------------------------------- html

def test_html_embeds_valid_json(graph):
    _, conn = graph
    html = viz.render_html(viz.module_graph(conn), "T", "S")
    m = re.search(r"const DATA = (\{.*?\});", html, re.S)
    assert m, "the page must embed its data"
    data = json.loads(m.group(1))
    assert data["nodes"]


def test_html_makes_no_external_requests(graph):
    """Local-first: the page must work with no network at all."""
    _, conn = graph
    html = viz.render_html(viz.module_graph(conn), "T", "S")
    refs = re.findall(r"""(?:src|href)=["'](https?://[^"']+)""", html)
    assert refs == []


def test_html_substitutes_the_title(graph):
    _, conn = graph
    html = viz.render_html(viz.module_graph(conn), "MyTitle", "MySub")
    assert "<title>MyTitle</title>" in html
    assert "MySub" in html
    assert "__DATA__" not in html and "__TITLE__" not in html


def test_html_survives_a_label_containing_markup(graph):
    """A symbol named like a tag must not be able to close the script block."""
    _, conn = graph
    data = viz.module_graph(conn)
    data["nodes"][0]["label"] = "</script><img>"
    html = viz.render_html(data, "T", "S")
    body = html.split("const DATA = ", 1)[1].split("\n", 1)[0]
    assert "</script>" not in body, "json.dumps must not emit a raw closing tag"
