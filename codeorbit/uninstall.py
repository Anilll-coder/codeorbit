"""Remove a CodeOrbit installation.

This deletes things, and it is asked to delete the very environment it is
running from, so the guards matter more than the feature. Three of them:

  1. It refuses to touch anything that is not clearly a virtualenv. If CodeOrbit
     was pip-installed into the system Python, `sys.prefix` IS the system
     Python, and removing it would take the interpreter with it.
  2. It never removes source. An editable install points at a working copy;
     deleting the venv is right, deleting the checkout is not.
  3. Project indexes are left alone unless asked for explicitly - they are
     yours, cheap to rebuild, and never what someone means by "uninstall".

Windows cannot delete a running .exe, and the launcher being run lives inside
the directory being removed, so there the removal is handed to a short detached
process that waits for this one to exit first.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .db import DB_DIRNAME

# The fences install.sh writes around the line it appends to a shell rc file.
# Must stay byte-identical to the ones in that script - they are how each side
# recognises the other's work.
RC_MARK_BEGIN = "# >>> codeorbit >>>"
RC_MARK_END = "# <<< codeorbit <<<"


@dataclass
class Plan:
    venv: Path | None = None
    launchers: list[Path] = field(default_factory=list)
    index: Path | None = None
    path_entry: str | None = None          # Windows user PATH entry to unset
    rc_files: list[Path] = field(default_factory=list)   # POSIX rc files to edit
    refusal: str | None = None             # set when uninstall must not proceed
    editable_source: Path | None = None    # a checkout we must NOT delete
    temp_files: list[Path] = field(default_factory=list)
    mcp_configs: list[Path] = field(default_factory=list)

    @property
    def anything(self) -> bool:
        return bool(self.venv or self.launchers or self.index or self.path_entry
                    or self.rc_files or self.temp_files or self.mcp_configs)


def temp_artifacts() -> list[Path]:
    """Everything CodeOrbit leaves in the system temp directory.

    Named patterns only - never a blanket sweep of TEMP, which holds other
    programs' work. Each of these is written by a specific code path:
      codeorbit-upgrade.log      the detached upgrade's output
      codeorbit-upgrade-*.ps1    the detached upgrade helper itself
      codeorbit-verify-*         `fix --test` sandboxes
      codeorbit-<hash>           installer clones, when it fetched the source
    """
    import tempfile
    tmp = Path(tempfile.gettempdir())
    found: list[Path] = []
    if not tmp.is_dir():
        return found

    patterns = ["codeorbit-upgrade.log", "codeorbit-upgrade-*.ps1",
                "codeorbit-verify-*", "codeorbit-*"]
    seen: set[Path] = set()
    for pat in patterns:
        try:
            for p in tmp.glob(pat):
                if p not in seen:
                    seen.add(p)
                    found.append(p)
        except OSError:
            continue
    return sorted(found)


def mcp_registrations(project: Path | None) -> list[Path]:
    """Agent config files that still name CodeOrbit.

    Leaving these behind is not cosmetic: the agent goes on spawning a command
    that no longer exists and reports the server as failed on every start. An
    uninstall that leaves an agent permanently erroring has not uninstalled.
    """
    from . import mcp_config

    candidates: list[Path] = []
    for agent, target in mcp_config.TARGETS.items():
        if project is not None:
            candidates.append(project / target.project_rel)
        if target.global_rel:
            candidates.append(Path.home() / target.global_rel)

    out: list[Path] = []
    for path in candidates:
        if path in out or not path.exists():
            continue
        try:
            import json
            data = json.loads(path.read_text(encoding="utf-8") or "{}")
        except Exception:
            continue
        if isinstance(data, dict) and \
                mcp_config.SERVER_KEY in (data.get("mcpServers") or {}):
            out.append(path)
    return out


def _is_virtualenv(prefix: Path) -> bool:
    return (prefix / "pyvenv.cfg").exists()


def _bin_dir(prefix: Path) -> Path:
    return prefix / ("Scripts" if os.name == "nt" else "bin")


def _editable_source() -> Path | None:
    """The working copy an editable install points at, if this is one."""
    try:
        import codeorbit
        pkg = Path(codeorbit.__file__).resolve().parent
    except Exception:
        return None
    # An editable install leaves the package inside its checkout, which is
    # outside site-packages.
    if "site-packages" in pkg.parts:
        return None
    root = pkg.parent
    return root if (root / "pyproject.toml").exists() else None


def _points_at(launcher: Path, venv: Path) -> bool:
    """Does this shim actually launch the venv we are removing?

    Load-bearing. Both installers write a tiny shim containing the absolute path
    of the interpreter it runs, and a machine can hold more than one install -
    a throwaway one under a temp directory, a real one under LOCALAPPDATA. An
    earlier version collected every shim in every known location and deleted
    them all, so uninstalling one install broke the other. A shim is only ours
    if it names our venv.
    """
    try:
        text = launcher.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    body = text.lower().replace("\\", "/")
    venv_s = str(venv).lower().replace("\\", "/")

    if venv_s in body:
        return True

    # install.sh runs under Git Bash and writes a POSIX path (/tmp/unitest/...)
    # while sys.prefix is the Windows form (C:/Users/.../Temp/unitest). Those
    # never compare equal, so a launcher written by install.sh was left behind
    # by an uninstall started from a Windows Python. Fall back to matching the
    # venv's own directory name followed by its bin directory, which both forms
    # share.
    name = venv.name.lower()
    return f"/{name}/scripts/" in body or f"/{name}/bin/" in body


def _launchers(venv: Path | None = None) -> list[Path]:
    """Shim scripts that launch `venv`, wherever an installer put them."""
    found: list[Path] = []
    candidates = [
        Path(os.environ["CODEORBIT_BIN"]) if os.environ.get("CODEORBIT_BIN") else None,
        Path.home() / ".local" / "bin",
        (Path(os.environ["LOCALAPPDATA"]) / "Programs" / "CodeOrbit" / "bin"
         if os.environ.get("LOCALAPPDATA") else None),
    ]
    seen: set[Path] = set()
    for d in candidates:
        if not d or not d.is_dir():
            continue
        for name in ("codeorbit", "codeorbit.cmd", "codeorbit.exe"):
            p = d / name
            if not p.exists() or p in seen:
                continue
            if venv is not None and not _points_at(p, venv):
                continue        # belongs to a different install; leave it alone
            seen.add(p)
            found.append(p)
    return found


def _rc_candidates() -> list[Path]:
    """Shell rc files install.sh may have appended a PATH line to."""
    home = Path.home()
    zdotdir = os.environ.get("ZDOTDIR")
    paths = [
        home / ".bashrc",
        home / ".bash_profile",
        home / ".zshrc",
        Path(zdotdir) / ".zshrc" if zdotdir else home / ".zshrc",
        home / ".profile",
        home / ".config" / "fish" / "config.fish",
    ]
    seen: set[Path] = set()
    out: list[Path] = []
    for p in paths:
        if p in seen:
            continue
        seen.add(p)
        out.append(p)
    return out


def rc_files_with_block() -> list[Path]:
    """The rc files that actually carry our fenced block right now."""
    found: list[Path] = []
    for p in _rc_candidates():
        try:
            if p.is_file() and RC_MARK_BEGIN in p.read_text(encoding="utf-8", errors="replace"):
                found.append(p)
        except OSError:
            continue
    return found


def strip_rc_block(path: Path) -> bool:
    """Delete our fenced block from `path`. Returns True if anything changed.

    Line-based and fence-anchored on purpose: this is someone's shell rc, and
    the only lines it may touch are the ones the installer put there. Anything
    it did not write - including a hand-edited PATH line outside the fences -
    is left exactly as found.
    """
    try:
        original = path.read_text(encoding="utf-8")
    except OSError:
        return False

    lines = original.splitlines(keepends=True)
    kept: list[str] = []
    inside = False
    changed = False
    for line in lines:
        stripped = line.strip()
        if not inside and stripped == RC_MARK_BEGIN:
            inside = True
            changed = True
            # install.sh writes a blank line before the fence; take it back so
            # repeated install/uninstall cycles do not grow the file.
            if kept and not kept[-1].strip():
                kept.pop()
            continue
        if inside:
            if stripped == RC_MARK_END:
                inside = False
            continue
        kept.append(line)

    if not changed:
        return False
    # An unterminated block means the file was edited by hand between the
    # fences. Refuse rather than swallow the rest of the file.
    if inside:
        return False

    try:
        shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
    except OSError:
        pass
    try:
        path.write_text("".join(kept), encoding="utf-8")
    except OSError:
        return False
    return True


def build_plan(project: Path | None = None, with_index: bool = False) -> Plan:
    plan = Plan()
    prefix = Path(sys.prefix).resolve()

    if not _is_virtualenv(prefix):
        plan.refusal = (
            "CodeOrbit is not installed in its own virtualenv - it is in the "
            f"Python at {prefix}.\n"
            "Removing that would take the interpreter with it, so this refuses.\n"
            "Uninstall the package instead:  pip uninstall codeorbit"
        )
        return plan

    plan.venv = prefix
    plan.editable_source = _editable_source()
    plan.launchers = _launchers(prefix)

    if os.name == "nt" and plan.launchers:
        # Only give up a PATH entry that one of OUR shims lived in.
        plan.path_entry = str(plan.launchers[0].parent)
    elif os.name != "nt":
        # The POSIX installer has no registry to edit, so it appends a fenced
        # block to a shell rc file instead. Scanned by content, not guessed
        # from the launcher: the user may have moved to a different shell
        # since installing.
        plan.rc_files = rc_files_with_block()

    if with_index and project is not None:
        idx = project / DB_DIRNAME
        if idx.is_dir():
            plan.index = idx

    plan.temp_files = temp_artifacts()
    plan.mcp_configs = mcp_registrations(project)

    return plan


def _detached_rmtree(target: Path, wait_for_pid: int) -> None:
    """Delete `target` after this process exits. Windows locks a running .exe."""
    if os.name == "nt":
        script = (
            "$p={pid}\n"
            "try {{ Wait-Process -Id $p -Timeout 30 -ErrorAction SilentlyContinue }} catch {{}}\n"
            "Start-Sleep -Milliseconds 500\n"
            "Remove-Item -LiteralPath '{target}' -Recurse -Force -ErrorAction SilentlyContinue\n"
        ).format(pid=wait_for_pid, target=str(target))
        fh = tempfile.NamedTemporaryFile("w", suffix=".ps1", delete=False,
                                         encoding="utf-8")
        fh.write(script)
        fh.close()
        subprocess.Popen(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", fh.name],
            # CREATE_NO_WINDOW, not DETACHED_PROCESS: the latter gives the
            # child no console, PowerShell then fails to initialise, and the
            # scheduled removal silently never happens - the venv survived an
            # uninstall that reported success.
            creationflags=0x08000000 | 0x00000200,   # CREATE_NO_WINDOW | NEW_GROUP
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    else:
        subprocess.Popen(
            ["sh", "-c", f'sleep 1; rm -rf "{target}"'],
            start_new_session=True,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


def remove_from_windows_path(entry: str) -> bool:
    """Drop `entry` from the user PATH. Returns True if it was there."""
    if os.name != "nt":
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0,
                            winreg.KEY_READ | winreg.KEY_WRITE) as key:
            try:
                current, kind = winreg.QueryValueEx(key, "Path")
            except FileNotFoundError:
                return False
            parts = [p for p in current.split(";") if p]
            keep = [p for p in parts if p.rstrip("\\") != entry.rstrip("\\")]
            if len(keep) == len(parts):
                return False
            winreg.SetValueEx(key, "Path", 0, kind, ";".join(keep))
            return True
    except Exception:
        return False


def execute(plan: Plan) -> list[str]:
    """Carry out `plan`. Returns human-readable lines describing what happened."""
    from . import mcp_config

    done: list[str] = []

    # Agent configs first, while CodeOrbit still exists: removing our entry is
    # then a clean edit. Do it after deleting the binary and the agent has
    # already started reporting a failed server.
    #
    # Edited directly rather than through mcp_config.remove(), because these
    # paths were found by scanning and need not match a known agent layout -
    # only our own key is touched either way.
    for cfg in plan.mcp_configs:
        try:
            import json
            data = json.loads(cfg.read_text(encoding="utf-8") or "{}")
            servers = data.get("mcpServers") or {}
            if mcp_config.SERVER_KEY not in servers:
                continue
            backup = cfg.with_suffix(cfg.suffix + ".bak")
            try:
                shutil.copy2(cfg, backup)
            except OSError:
                pass
            del servers[mcp_config.SERVER_KEY]
            cfg.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
            others = ", ".join(sorted(servers)) or "none"
            done.append(f"removed the CodeOrbit entry from {cfg} "
                        f"(other servers kept: {others})")
        except Exception as e:
            done.append(f"could not clean {cfg}: {e}")

    for t in plan.temp_files:
        try:
            if t.is_dir():
                shutil.rmtree(t, ignore_errors=True)
            else:
                t.unlink(missing_ok=True)
        except OSError:
            continue
    if plan.temp_files:
        done.append(f"removed {len(plan.temp_files)} temporary file(s) from TEMP")

    if plan.index is not None:
        shutil.rmtree(plan.index, ignore_errors=True)
        done.append(f"removed index {plan.index}")

    for p in plan.launchers:
        try:
            p.unlink()
            done.append(f"removed launcher {p}")
        except OSError as e:
            done.append(f"could not remove {p}: {e}")

    if plan.path_entry and remove_from_windows_path(plan.path_entry):
        done.append(f"removed {plan.path_entry} from your user PATH")

    for rc in plan.rc_files:
        if strip_rc_block(rc):
            done.append(f"removed the PATH line from {rc}")
        else:
            done.append(f"could not clean {rc} - remove the codeorbit block by hand")

    if plan.venv is not None:
        running_from_venv = Path(sys.executable).resolve().is_relative_to(plan.venv)
        if running_from_venv:
            _detached_rmtree(plan.venv, os.getpid())
            done.append(f"scheduled removal of {plan.venv} (it is running right now)")
        else:
            shutil.rmtree(plan.venv, ignore_errors=True)
            done.append(f"removed {plan.venv}")

    return done
