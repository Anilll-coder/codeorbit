"""Write CodeOrbit into an agent's MCP config file.

This edits a file the user did not create and may share with other tools, so
the rules are narrow on purpose:

  * merge, never replace - other MCP servers in the file are left byte-identical
  * idempotent - running twice changes nothing the second time
  * reversible - `--remove` takes only our entry back out
  * back up before the first overwrite, so a malformed file is recoverable

The command written is an ABSOLUTE path to a real executable. An agent spawns
the server as a bare subprocess with no shell and no PATH lookup of its own, so
a plain "codeorbit" resolves to nothing - and on Windows the entry point is
codeorbit.exe while the installed launcher is codeorbit.cmd, neither of which a
bare name finds.
"""
from __future__ import annotations

import json
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

SERVER_KEY = "codeorbit"


@dataclass
class Target:
    name: str
    description: str
    # Where the file lives, relative to the project root (or home, if global).
    project_rel: str
    global_rel: str | None = None


TARGETS = {
    "claude": Target(
        "Claude Code", "project-scoped .mcp.json, committed with the repo",
        ".mcp.json", None),
    "cursor": Target(
        "Cursor", ".cursor/mcp.json",
        ".cursor/mcp.json", ".cursor/mcp.json"),
    "windsurf": Target(
        "Windsurf", "~/.codeium/windsurf/mcp_config.json",
        ".windsurf/mcp.json", ".codeium/windsurf/mcp_config.json"),
}


@dataclass
class Result:
    path: Path
    action: str            # created | updated | unchanged | removed | absent
    backup: Path | None = None
    others: list[str] = None    # other servers found in the file, untouched


def find_executable() -> str:
    """An absolute path to something an agent can actually spawn."""
    found = shutil.which(SERVER_KEY)
    if found and Path(found).exists():
        return str(Path(found).resolve())
    here = Path(sys.executable).parent
    for name in ("codeorbit.exe", "codeorbit.cmd", "codeorbit"):
        cand = here / name
        if cand.exists():
            return str(cand.resolve())
    return SERVER_KEY


def server_entry(root: Path, exe: str | None = None) -> dict:
    return {
        "command": exe or find_executable(),
        "args": ["mcp", "--path", str(root)],
    }


def config_path(agent: str, root: Path, use_global: bool = False) -> Path:
    target = TARGETS[agent]
    if use_global:
        if not target.global_rel:
            raise ValueError(f"{target.name} has no global config location")
        return Path.home() / target.global_rel
    return root / target.project_rel


def _load(path: Path) -> tuple[dict, str | None]:
    """Return (config, error). A broken file is reported, never silently reset."""
    if not path.exists():
        return {}, None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        return {}, f"could not read {path}: {e}"
    if not text.strip():
        return {}, None
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        return {}, (f"{path} is not valid JSON ({e}). Fix or move it first - "
                    "refusing to overwrite a file that may hold your other "
                    "MCP servers.")
    if not isinstance(data, dict):
        return {}, f"{path} does not contain a JSON object."
    return data, None


def install(agent: str, root: Path, use_global: bool = False,
            exe: str | None = None) -> tuple[Result | None, str | None]:
    path = config_path(agent, root, use_global)
    data, err = _load(path)
    if err:
        return None, err

    servers = data.get("mcpServers")
    if servers is None:
        servers = {}
        data["mcpServers"] = servers
    elif not isinstance(servers, dict):
        return None, f'"mcpServers" in {path} is not an object; refusing to touch it.'

    entry = server_entry(root, exe)
    others = sorted(k for k in servers if k != SERVER_KEY)

    if servers.get(SERVER_KEY) == entry:
        return Result(path, "unchanged", None, others), None

    action = "updated" if SERVER_KEY in servers else "created"
    backup = None
    if path.exists():
        backup = path.with_suffix(path.suffix + ".bak")
        try:
            shutil.copy2(path, backup)
        except OSError:
            backup = None
    else:
        action = "created"

    servers[SERVER_KEY] = entry
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return Result(path, action, backup, others), None


def remove(agent: str, root: Path, use_global: bool = False
           ) -> tuple[Result | None, str | None]:
    path = config_path(agent, root, use_global)
    if not path.exists():
        return Result(path, "absent", None, []), None

    data, err = _load(path)
    if err:
        return None, err

    servers = data.get("mcpServers")
    if not isinstance(servers, dict) or SERVER_KEY not in servers:
        return Result(path, "absent", None,
                      sorted(servers) if isinstance(servers, dict) else []), None

    backup = path.with_suffix(path.suffix + ".bak")
    try:
        shutil.copy2(path, backup)
    except OSError:
        backup = None

    del servers[SERVER_KEY]
    others = sorted(servers)
    # Leave the file in place even when empty: it may be committed, and deleting
    # a file we did not create is not ours to do.
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return Result(path, "removed", backup, others), None
