"""Tests for writing CodeOrbit into an agent's MCP config.

This edits a file the user did not create and may share with other tools, so
what is pinned is restraint: other servers survive byte-for-byte, a second run
changes nothing, removal takes only our entry, and a file we cannot parse is
refused rather than overwritten.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from codeorbit import cli, mcp_config

runner = CliRunner()

OTHER = {"command": "othertool", "args": ["serve"]}


@pytest.fixture(autouse=True)
def _reset_shared_path():
    cli._shared["path"] = None
    yield
    cli._shared["path"] = None


def write_config(root: Path, servers: dict) -> Path:
    p = root / ".mcp.json"
    p.write_text(json.dumps({"mcpServers": servers}, indent=2), encoding="utf-8")
    return p


def read_config(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


# ------------------------------------------------------------------ writing

def test_creates_the_file_when_absent(tmp_path: Path):
    result, err = mcp_config.install("claude", tmp_path)
    assert err is None
    assert result.action == "created"
    data = read_config(result.path)
    assert data["mcpServers"]["codeorbit"]["args"][:2] == ["mcp", "--path"]


def test_merges_without_disturbing_other_servers(tmp_path: Path):
    path = write_config(tmp_path, {"some-other-tool": OTHER})
    result, err = mcp_config.install("claude", tmp_path)
    assert err is None

    data = read_config(path)
    assert data["mcpServers"]["some-other-tool"] == OTHER, "another server was changed"
    assert "codeorbit" in data["mcpServers"]
    assert result.others == ["some-other-tool"]


def test_running_twice_changes_nothing(tmp_path: Path):
    first, _ = mcp_config.install("claude", tmp_path)
    before = first.path.read_text(encoding="utf-8")

    second, err = mcp_config.install("claude", tmp_path)
    assert err is None
    assert second.action == "unchanged"
    assert second.path.read_text(encoding="utf-8") == before


def test_an_existing_file_is_backed_up_before_the_first_write(tmp_path: Path):
    path = write_config(tmp_path, {"some-other-tool": OTHER})
    result, _ = mcp_config.install("claude", tmp_path)
    assert result.backup and result.backup.exists()
    assert "some-other-tool" in result.backup.read_text(encoding="utf-8")


def test_the_command_written_is_an_absolute_path(tmp_path: Path):
    """An agent spawns this with no shell and no PATH lookup of its own."""
    result, _ = mcp_config.install("claude", tmp_path)
    cmd = read_config(result.path)["mcpServers"]["codeorbit"]["command"]
    assert Path(cmd).is_absolute() or cmd == "codeorbit"


def test_the_project_path_is_baked_into_the_args(tmp_path: Path):
    result, _ = mcp_config.install("claude", tmp_path)
    args = read_config(result.path)["mcpServers"]["codeorbit"]["args"]
    assert str(tmp_path.resolve()) in args


# ------------------------------------------------------------------ refusal

def test_malformed_json_is_refused_not_overwritten(tmp_path: Path):
    """The file may hold the user's other servers; never reset it."""
    path = tmp_path / ".mcp.json"
    path.write_text("{ this is not json", encoding="utf-8")

    result, err = mcp_config.install("claude", tmp_path)
    assert result is None
    assert err and "not valid JSON" in err
    assert path.read_text(encoding="utf-8") == "{ this is not json", "file was modified"


def test_a_non_object_mcpServers_is_refused(tmp_path: Path):
    path = tmp_path / ".mcp.json"
    path.write_text(json.dumps({"mcpServers": ["not", "an", "object"]}), encoding="utf-8")
    result, err = mcp_config.install("claude", tmp_path)
    assert result is None
    assert err and "not an object" in err


def test_an_empty_file_is_treated_as_no_config(tmp_path: Path):
    (tmp_path / ".mcp.json").write_text("", encoding="utf-8")
    result, err = mcp_config.install("claude", tmp_path)
    assert err is None
    assert "codeorbit" in read_config(result.path)["mcpServers"]


# ------------------------------------------------------------------ removal

def test_remove_takes_only_our_entry(tmp_path: Path):
    write_config(tmp_path, {"some-other-tool": OTHER})
    mcp_config.install("claude", tmp_path)

    result, err = mcp_config.remove("claude", tmp_path)
    assert err is None
    assert result.action == "removed"

    data = read_config(result.path)
    assert data["mcpServers"] == {"some-other-tool": OTHER}


def test_remove_is_a_no_op_when_absent(tmp_path: Path):
    write_config(tmp_path, {"some-other-tool": OTHER})
    result, err = mcp_config.remove("claude", tmp_path)
    assert err is None
    assert result.action == "absent"
    assert read_config(result.path)["mcpServers"] == {"some-other-tool": OTHER}


def test_remove_on_a_missing_file_is_not_an_error(tmp_path: Path):
    result, err = mcp_config.remove("claude", tmp_path)
    assert err is None
    assert result.action == "absent"


# --------------------------------------------------------------- locations

def test_each_agent_has_its_own_config_location(tmp_path: Path):
    seen = {mcp_config.config_path(a, tmp_path) for a in mcp_config.TARGETS}
    assert len(seen) == len(mcp_config.TARGETS), "two agents share a path"


def test_global_config_lives_under_home(tmp_path: Path):
    p = mcp_config.config_path("cursor", tmp_path, use_global=True)
    assert str(p).startswith(str(Path.home()))


def test_claude_has_no_global_location(tmp_path: Path):
    with pytest.raises(ValueError):
        mcp_config.config_path("claude", tmp_path, use_global=True)


# --------------------------------------------------------------------- CLI

def test_cli_writes_the_config(tmp_path: Path):
    r = runner.invoke(cli.app, ["install-mcp", "-p", str(tmp_path)])
    assert r.exit_code == 0, r.output
    assert (tmp_path / ".mcp.json").exists()
    assert "Wrote" in r.output


def test_cli_print_changes_nothing(tmp_path: Path):
    r = runner.invoke(cli.app, ["install-mcp", "-p", str(tmp_path), "--print"])
    assert r.exit_code == 0
    assert not (tmp_path / ".mcp.json").exists(), "--print must not write"
    assert "mcpServers" in r.output


def test_cli_remove(tmp_path: Path):
    runner.invoke(cli.app, ["install-mcp", "-p", str(tmp_path)])
    r = runner.invoke(cli.app, ["install-mcp", "-p", str(tmp_path), "--remove"])
    assert r.exit_code == 0
    assert "Removed" in r.output
    assert "codeorbit" not in read_config(tmp_path / ".mcp.json")["mcpServers"]


def test_cli_rejects_an_unknown_agent(tmp_path: Path):
    r = runner.invoke(cli.app, ["install-mcp", "-p", str(tmp_path), "-a", "nope"])
    assert r.exit_code == 1
    assert "Unknown agent" in r.output


def test_cli_warns_when_the_project_is_not_indexed(tmp_path: Path):
    r = runner.invoke(cli.app, ["install-mcp", "-p", str(tmp_path)])
    assert "not indexed yet" in r.output
