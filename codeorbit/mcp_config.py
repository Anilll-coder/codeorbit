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
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

SERVER_KEY = "codeorbit"


def _platform_app_dir(app: str) -> Path:
    """Where a desktop app on this OS keeps its user-level config directory.

    The same three-way branch essentially every desktop app follows:
    %APPDATA% on Windows, ~/Library/Application Support on macOS, and the XDG
    convention (~/.config, or $XDG_CONFIG_HOME if set) everywhere else. Getting
    this wrong does not fail loudly - it writes into a file the app never
    reads, and a user-scoped install that "worked" silently does nothing.
    """
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / app
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / app
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / app


@dataclass
class Target:
    name: str
    description: str
    # Where the file lives, relative to the project root. None means this
    # agent has no project-scoped config at all (Claude Desktop): only the
    # global location applies, regardless of --global.
    project_rel: str | None
    # Relative to $HOME, for agents whose global config sits at a fixed path
    # under it (Cursor, Windsurf). Ignored when global_path is set.
    global_rel: str | None = None
    # For agents whose global config location is OS-specific (VS Code, Claude
    # Desktop) rather than a fixed path under $HOME.
    global_path: Callable[[], Path] | None = None
    # The top-level key the server entry is nested under. Every target here
    # uses "mcpServers" except VS Code, which uses "servers" - one real
    # difference in an otherwise identical file shape.
    servers_key: str = "mcpServers"


TARGETS = {
    "claude": Target(
        "Claude Code", "project-scoped .mcp.json, committed with the repo",
        ".mcp.json", None),
    "claude-desktop": Target(
        "Claude Desktop", "the desktop app's global config - it has no "
        "per-project file, so this always writes there",
        None, global_path=lambda: _platform_app_dir("Claude") / "claude_desktop_config.json"),
    "cursor": Target(
        "Cursor", ".cursor/mcp.json",
        ".cursor/mcp.json", ".cursor/mcp.json"),
    "windsurf": Target(
        "Windsurf", "~/.codeium/windsurf/mcp_config.json",
        ".windsurf/mcp.json", ".codeium/windsurf/mcp_config.json"),
    "vscode": Target(
        "VS Code", "project-scoped .vscode/mcp.json, or the user config",
        ".vscode/mcp.json",
        global_path=lambda: _platform_app_dir("Code") / "User" / "mcp.json",
        servers_key="servers"),
}


@dataclass
class Result:
    path: Path
    action: str            # created | updated | unchanged | removed | absent
    backup: Path | None = None
    others: list[str] = None    # other servers found in the file, untouched


def find_executable() -> str:
    """An absolute path to something an agent can actually spawn.

    The real entry point beside this interpreter comes FIRST, ahead of a PATH
    lookup. Two reasons, both learned from a config that did not work:

      * PATH resolves to the installer's `codeorbit.cmd` shim, and a .cmd is a
        batch file - it needs cmd.exe to interpret it. An MCP client spawns the
        command directly, with no shell, so a .cmd entry can simply fail to
        start.
      * The entry point next to sys.executable is the one belonging to THIS
        install, which is what should serve the graph.
    """
    here = Path(sys.executable).parent
    for name in ("codeorbit.exe", "codeorbit"):
        cand = here / name
        if cand.exists() and cand.is_file():
            return str(cand.resolve())

    found = shutil.which(SERVER_KEY)
    if found and Path(found).exists():
        resolved = Path(found).resolve()
        # A .cmd shim names its real target; prefer that.
        if resolved.suffix.lower() == ".cmd":
            real = _target_of_cmd_shim(resolved)
            if real:
                return str(real)
        return str(resolved)

    return SERVER_KEY


def _target_of_cmd_shim(shim: Path) -> Path | None:
    """The .exe a generated `codeorbit.cmd` launcher points at."""
    try:
        for line in shim.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip().strip('"')
            if line.lower().endswith(".exe") or ".exe" in line.lower():
                start = line.find('"')
                if start != -1:
                    end = line.find('"', start + 1)
                    cand = Path(line[start + 1:end]) if end != -1 else None
                else:
                    cand = Path(line.split(" %*")[0])
                if cand and cand.exists():
                    return cand.resolve()
    except OSError:
        pass
    return None


def server_entry(root: Path, exe: str | None = None) -> dict:
    """The config block an agent needs to launch this server.

    `type: "stdio"` is stated explicitly rather than left to be inferred.
    Claude Code infers it from the presence of `command`, but Cursor's agent
    discriminates on the field: without it, it treated the entry as a remote
    server, initialised an OAuth provider for it, and never spawned the process
    at all - reporting only "not connected" with no underlying error.
    """
    return {
        "type": "stdio",
        "command": exe or find_executable(),
        "args": ["mcp", "--path", str(root)],
    }


def _same_path(a: object, b: object) -> bool:
    """Do two path strings name the same file, allowing for spelling?"""
    if not isinstance(a, str) or not isinstance(b, str):
        return a == b
    try:
        # normcase folds case AND turns / into \ on Windows; it is a no-op
        # elsewhere, where paths really are case-sensitive.
        return (os.path.normcase(os.path.normpath(a))
                == os.path.normcase(os.path.normpath(b)))
    except (TypeError, ValueError):
        return a == b


def entry_equivalent(existing: object, wanted: dict) -> bool:
    """True when `existing` already launches exactly the server `wanted` does.

    Compared by meaning, not by spelling, and this matters more than it looks.
    The same install produces different strings on different runs - `c:\\proj`
    vs `C:\\Proj`, forward slashes vs back - and rewriting the entry over a
    cosmetic difference is not free:

      Cursor's agent gates every MCP server behind an approval keyed to
      `sha256({path: <project root>, server: <this exact object>})`. Rewriting
      the entry changes that hash, which silently revokes the approval the user
      already gave. The next session then refuses to load the server, while
      `cursor-agent mcp list` - which does not check approvals - still shows it
      and lists its tools. That split is the whole bug: it reads as a broken
      connection and is really a withdrawn consent.

    So a rewrite has a cost, and is worth paying only for a real change. A
    changed `type`, a different executable, or different arguments all still
    count as real. A dead `command` does too - an install that moved must be
    repaired even though that costs an approval.
    """
    if not isinstance(existing, dict):
        return False
    if existing.get("type") != wanted.get("type"):
        return False

    cmd = existing.get("command")
    if not _same_path(cmd, wanted.get("command")):
        return False
    if not isinstance(cmd, str) or not Path(cmd).exists():
        return False

    old_args, new_args = existing.get("args"), wanted.get("args")
    if not isinstance(old_args, list) or len(old_args) != len(new_args):
        return False
    return all(o == n or _same_path(o, n) for o, n in zip(old_args, new_args))


def config_path(agent: str, root: Path, use_global: bool = False) -> Path:
    target = TARGETS[agent]
    # A target with no project-scoped file at all (Claude Desktop) has exactly
    # one sensible location, so --global is implied rather than required: the
    # alternative is a footgun where the natural first command - no flags -
    # fails for a reason the user has no way to guess.
    want_global = use_global or target.project_rel is None
    if not want_global:
        return root / target.project_rel
    if target.global_path is not None:
        return target.global_path()
    if not target.global_rel:
        raise ValueError(f"{target.name} has no global config location")
    return Path.home() / target.global_rel


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
    try:
        path = config_path(agent, root, use_global)
    except ValueError as e:
        return None, str(e)
    key = TARGETS[agent].servers_key
    data, err = _load(path)
    if err:
        return None, err

    servers = data.get(key)
    if servers is None:
        servers = {}
        data[key] = servers
    elif not isinstance(servers, dict):
        return None, f'"{key}" in {path} is not an object; refusing to touch it.'

    entry = server_entry(root, exe)
    others = sorted(k for k in servers if k != SERVER_KEY)

    current = servers.get(SERVER_KEY)
    if current == entry or entry_equivalent(current, entry):
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
    try:
        path = config_path(agent, root, use_global)
    except ValueError as e:
        return None, str(e)
    key = TARGETS[agent].servers_key
    if not path.exists():
        return Result(path, "absent", None, []), None

    data, err = _load(path)
    if err:
        return None, err

    servers = data.get(key)
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
