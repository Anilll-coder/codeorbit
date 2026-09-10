"""Rewriting an MCP entry has a cost, so an equivalent one must not be rewritten.

Cursor keys its MCP approval to a hash of the exact server object. A rewrite
over a cosmetic path difference silently revokes the user's approval, after
which every session refuses the server while `cursor-agent mcp list` still
shows it - which reads as a broken connection rather than a withdrawn consent.
"""
from __future__ import annotations

import json
import os
import sys

import pytest

from codeorbit import mcp_config


def _write(path, entry):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"mcpServers": {"codeorbit": entry}}, indent=2),
                    encoding="utf-8")


def test_identical_entry_is_unchanged(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    entry = mcp_config.server_entry(root, str(sys.executable))
    _write(root / ".cursor" / "mcp.json", entry)

    result, err = mcp_config.install("cursor", root, exe=str(sys.executable))
    assert err is None
    assert result.action == "unchanged"


@pytest.mark.skipif(os.name != "nt", reason="path case only folds on Windows")
def test_windows_path_spelling_does_not_rewrite(tmp_path):
    """`c:\\x` and `C:\\X`, `/` and `\\`, all name the same server."""
    root = tmp_path / "proj"
    root.mkdir()
    exe = str(sys.executable)

    stored = mcp_config.server_entry(root, exe)
    # The spelling a different run could plausibly produce.
    stored = {
        "type": stored["type"],
        "command": exe.replace("\\", "/"),
        "args": ["mcp", "--path", str(root).lower()],
    }
    _write(root / ".cursor" / "mcp.json", stored)

    result, err = mcp_config.install("cursor", root, exe=exe)
    assert err is None
    assert result.action == "unchanged"
    # and the file was genuinely left alone
    on_disk = json.loads((root / ".cursor" / "mcp.json").read_text(encoding="utf-8"))
    assert on_disk["mcpServers"]["codeorbit"] == stored


def test_real_change_still_rewrites(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    _write(root / ".cursor" / "mcp.json",
           {"type": "stdio", "command": str(sys.executable),
            "args": ["mcp", "--path", str(tmp_path / "somewhere-else")]})

    result, err = mcp_config.install("cursor", root, exe=str(sys.executable))
    assert err is None
    assert result.action == "updated"


def test_missing_type_is_a_real_change(tmp_path):
    """Adding `type: stdio` is worth one re-approval: without it Cursor
    treats the entry as a remote server and never spawns the process."""
    root = tmp_path / "proj"
    root.mkdir()
    _write(root / ".cursor" / "mcp.json",
           {"command": str(sys.executable), "args": ["mcp", "--path", str(root)]})

    result, err = mcp_config.install("cursor", root, exe=str(sys.executable))
    assert err is None
    assert result.action == "updated"
    entry = json.loads((root / ".cursor" / "mcp.json").read_text(
        encoding="utf-8"))["mcpServers"]["codeorbit"]
    assert entry["type"] == "stdio"


def test_dead_command_is_repaired(tmp_path):
    """An install that moved must be fixed even though that costs an approval."""
    root = tmp_path / "proj"
    root.mkdir()
    gone = tmp_path / "removed-venv" / "codeorbit.exe"
    _write(root / ".cursor" / "mcp.json",
           {"type": "stdio", "command": str(gone),
            "args": ["mcp", "--path", str(root)]})

    result, err = mcp_config.install("cursor", root, exe=str(sys.executable))
    assert err is None
    assert result.action == "updated"


def test_other_servers_survive_an_equivalent_noop(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    cfg = root / ".cursor" / "mcp.json"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(json.dumps({"mcpServers": {
        "codeorbit": mcp_config.server_entry(root, str(sys.executable)),
        "other": {"command": "something-else"},
    }}, indent=2), encoding="utf-8")

    result, err = mcp_config.install("cursor", root, exe=str(sys.executable))
    assert err is None
    assert result.action == "unchanged"
    assert result.others == ["other"]
    assert "other" in json.loads(cfg.read_text(encoding="utf-8"))["mcpServers"]
