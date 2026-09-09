"""MCP server tests, driven through a real client session over the real transport.

Nothing is stubbed at the protocol layer: each test starts the server exactly
as an agent would, completes the MCP handshake, and calls tools. A hand-rolled
check of the handler functions would pass while the server was unusable - which
is the only failure that matters here.
"""
from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from codeorbit.indexer import index_project
from codeorbit.resolve import resolve_project

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(scope="module")
def project(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("mcpproj")
    (root / "pkg").mkdir()
    (root / "pkg" / "core.py").write_text(textwrap.dedent('''
        import hashlib


        def helper(value):
            """Normalise a value."""
            return value.strip().lower()


        def digest(value):
            return hashlib.md5(value.encode()).hexdigest()


        def engine(value):
            return digest(helper(value))
    ''').lstrip(), encoding="utf-8")
    (root / "tests").mkdir()
    (root / "tests" / "test_core.py").write_text(
        "from pkg.core import engine\n\n\ndef test_engine():\n    assert engine(' A ')\n",
        encoding="utf-8")
    index_project(root)
    resolve_project(root)
    return root


def params(root: Path) -> StdioServerParameters:
    """Launch the server the way an agent does: a subprocess over stdio."""
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "codeorbit.cli", "mcp", "--path", str(root)],
        env=None,
    )


async def call(root: Path, tool: str, args: dict) -> str:
    async with stdio_client(params(root)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(tool, args)
            return "\n".join(c.text for c in result.content if hasattr(c, "text"))


# ------------------------------------------------------------------ protocol

async def test_handshake_and_tool_listing(project: Path):
    async with stdio_client(params(project)) as (read, write):
        async with ClientSession(read, write) as session:
            info = await session.initialize()
            assert info.server_info.name == "codeorbit"
            tools = await session.list_tools()
            names = {t.name for t in tools.tools}
            assert {
                "codeorbit_explore", "codeorbit_search", "codeorbit_node",
                "codeorbit_impact", "codeorbit_path", "codeorbit_audit",
                "codeorbit_overview",
            } <= names


async def test_every_tool_declares_a_schema_and_description(project: Path):
    async with stdio_client(params(project)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            for t in (await session.list_tools()).tools:
                assert t.description and len(t.description) > 40, t.name
                assert t.input_schema.get("type") == "object", t.name


# --------------------------------------------------------------------- tools

async def test_explore_returns_source_and_neighbourhood(project: Path):
    out = await call(project, "codeorbit_explore", {"query_text": "engine"})
    assert "engine" in out
    assert "def engine" in out, "the actual source must come back"
    assert "calls:" in out, "the call neighbourhood must come back"


async def test_search_lists_matches(project: Path):
    out = await call(project, "codeorbit_search", {"term": "helper"})
    assert "helper" in out and "core.py" in out


async def test_node_returns_one_symbol_in_full(project: Path):
    out = await call(project, "codeorbit_node", {"name": "helper"})
    assert "def helper" in out
    assert "called by" in out


async def test_impact_reports_reach_and_test_coverage(project: Path):
    out = await call(project, "codeorbit_impact", {"name": "helper"})
    assert "can affect" in out
    assert "test" in out.lower(), "test coverage must be reported either way"


async def test_path_walks_the_call_chain(project: Path):
    out = await call(project, "codeorbit_path", {"from_symbol": "engine", "to_symbol": "digest"})
    assert "engine" in out and "digest" in out
    assert "hop" in out


async def test_audit_ranks_by_reach(project: Path):
    out = await call(project, "codeorbit_audit", {"severity": "low"})
    assert "MD5" in out or "finding" in out.lower()


async def test_overview_names_the_most_depended_on(project: Path):
    out = await call(project, "codeorbit_overview", {})
    assert "symbols" in out
    assert "Most depended-on" in out


# ------------------------------------------------------- graceful conditions

async def test_missing_symbol_is_guidance_not_an_error(project: Path):
    """An agent that receives errors stops calling the tool at all."""
    async with stdio_client(params(project)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(
                "codeorbit_node", {"name": "no_such_symbol_anywhere"})
            assert not result.is_error, "an expected condition must not be an error"
            text = "\n".join(c.text for c in result.content if hasattr(c, "text"))
            assert "codeorbit_search" in text, "it should point at another tool"


async def test_unindexed_project_is_guidance_not_an_error(tmp_path: Path):
    async with stdio_client(params(tmp_path)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool("codeorbit_overview", {})
            assert not result.is_error
            text = "\n".join(c.text for c in result.content if hasattr(c, "text"))
            assert "codeorbit index" in text


async def test_no_tool_tells_the_agent_to_read_files(project: Path):
    """Steering an agent back to Read defeats the point of the graph."""
    for tool, args in [
        ("codeorbit_explore", {"query_text": "engine"}),
        ("codeorbit_node", {"name": "helper"}),
        ("codeorbit_search", {"term": "nothing_matches_this"}),
    ]:
        out = (await call(project, tool, args)).lower()
        assert "use read" not in out
        assert "read the file" not in out


async def test_project_path_overrides_the_launch_directory(project: Path, tmp_path: Path):
    """One server can answer for a different project than it was started in."""
    async with stdio_client(params(tmp_path)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(
                "codeorbit_search", {"term": "helper", "project_path": str(project)})
            text = "\n".join(c.text for c in result.content if hasattr(c, "text"))
            assert "helper" in text
