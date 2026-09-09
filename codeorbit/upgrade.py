"""Report what is installed, and upgrade it in place.

The motivating incident: an installed copy was several commits behind, its
`--help` was missing an option that had already been fixed in source, and there
was no way to see that from the outside - `codeorbit --version` did not exist,
so a stale install and a broken install looked identical.

Two things fix that. `--version` says what is running and from where. `upgrade`
reinstalls over the same virtualenv from wherever this copy came from.

Where it came from is not something pip records for a non-editable install, so
the installers write it down at install time (`.codeorbit-source` beside the
venv). Failing that: an editable install points at its own checkout, and a
plain install falls back to the public repo.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

DEFAULT_REPO = "https://github.com/Anilll-coder/codeorbit.git"
SOURCE_MARKER = ".codeorbit-source"


def version() -> str:
    try:
        import importlib.metadata as md
        return md.version("codeorbit")
    except Exception:
        return "unknown"


def install_root() -> Path:
    """The virtualenv this copy runs from."""
    return Path(sys.prefix).resolve()


def is_virtualenv() -> bool:
    return (install_root() / "pyvenv.cfg").exists()


def editable_source() -> Path | None:
    """The checkout an editable install points at, if this is one."""
    try:
        import codeorbit
        pkg = Path(codeorbit.__file__).resolve().parent
    except Exception:
        return None
    if "site-packages" in pkg.parts:
        return None
    root = pkg.parent
    return root if (root / "pyproject.toml").exists() else None


def recorded_source() -> str | None:
    """What the installer wrote down, if anything.

    Read as utf-8-sig, not utf-8. Windows PowerShell 5.1's Set-Content -Encoding
    utf8 writes a BOM, and a leading \\ufeff makes the recorded path fail
    exists() - so the git pull was skipped and pip was handed a path that could
    not resolve. The marker is also written BOM-free now, but installs made
    before that fix still have one.
    """
    marker = install_root() / SOURCE_MARKER
    if marker.exists():
        try:
            text = marker.read_text(encoding="utf-8-sig").strip().lstrip("﻿")
            return text or None
        except OSError:
            return None
    return None


def record_source(src: str) -> None:
    try:
        (install_root() / SOURCE_MARKER).write_text(src + "\n", encoding="utf-8")
    except OSError:
        pass


def resolve_source(explicit: str | None = None) -> tuple[str, str]:
    """Return (source, how_we_found_it)."""
    if explicit:
        return explicit, "given on the command line"
    ed = editable_source()
    if ed is not None:
        return str(ed), "this editable checkout"
    rec = recorded_source()
    if rec:
        return rec, f"recorded at install time ({SOURCE_MARKER})"
    return DEFAULT_REPO, "the default repository"


def launched_via_console_script() -> bool:
    """Were we started through codeorbit.exe / the console script?

    Only matters on Windows, where that .exe is held open by the running
    process and pip cannot replace it. On POSIX a running program's file can be
    replaced freely, so an in-process upgrade is safe there.
    """
    import os
    if os.name != "nt":
        return False
    try:
        argv0 = Path(sys.argv[0])
    except (IndexError, TypeError):
        return False
    if argv0.stem.lower() != "codeorbit":
        return False
    # The wrapper pip installs next to the interpreter is the locked file.
    return (Path(sys.executable).parent / "codeorbit.exe").exists()


def _is_self_lock(pip_output: str) -> bool:
    """Did pip fail because it could not replace our own running executable?"""
    low = pip_output.lower()
    return ("winerror 32" in low or "being used by another process" in low) \
        and "codeorbit" in low


def _spawn_detached_upgrade(source: str) -> Path | None:
    """Run the upgrade after this process exits. Returns the log path."""
    import os
    import tempfile

    log = Path(tempfile.gettempdir()) / "codeorbit-upgrade.log"
    py = sys.executable
    pid = os.getpid()

    if os.name == "nt":
        # `*>` redirection writes UTF-16 in PowerShell 5.1, which makes the log
        # unreadable to anything expecting text. Capture and write it explicitly.
        script = (
            f"$ErrorActionPreference='Continue'\n"
            f"try {{ Wait-Process -Id {pid} -Timeout 60 -ErrorAction SilentlyContinue }} catch {{}}\n"
            f"Start-Sleep -Milliseconds 800\n"
            f"$out = & '{py}' -m pip install --upgrade '{source}' 2>&1 | Out-String\n"
            f"$ver = & '{py}' -c \"import importlib.metadata as m; "
            f"print(m.version('codeorbit'))\" 2>&1 | Out-String\n"
            f"[System.IO.File]::WriteAllText('{log}', "
            f"$out + \"`nnow at \" + $ver.Trim() + \"`n\")\n"
        )
        fh = tempfile.NamedTemporaryFile("w", suffix=".ps1", delete=False,
                                         encoding="utf-8")
        fh.write(script)
        fh.close()
        try:
            # CREATE_NO_WINDOW, not DETACHED_PROCESS. DETACHED_PROCESS gives the
            # child no console at all, and PowerShell then fails to initialise
            # and exits without running a line - the upgrade silently did
            # nothing and left an empty log. CREATE_NO_WINDOW keeps it headless
            # while still letting it start.
            subprocess.Popen(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                 "-File", fh.name],
                creationflags=0x08000000 | 0x00000200,   # CREATE_NO_WINDOW | NEW_GROUP
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL)
            return log
        except OSError:
            return None

    try:
        subprocess.Popen(
            ["sh", "-c",
             f'sleep 1; "{py}" -m pip install --upgrade "{source}" > "{log}" 2>&1'],
            start_new_session=True,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL)
        return log
    except OSError:
        return None


@dataclass
class Outcome:
    ok: bool
    before: str
    after: str
    source: str
    detail: str = ""

    @property
    def changed(self) -> bool:
        return self.before != self.after


def _pip(*args: str, timeout: int = 900) -> tuple[int, str]:
    try:
        p = subprocess.run(
            [sys.executable, "-m", "pip", *args],
            capture_output=True, text=True, timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as e:
        return 1, str(e)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def git_pull(checkout: Path) -> tuple[bool, str]:
    """Update a local checkout before reinstalling from it."""
    if not shutil.which("git") or not (checkout / ".git").exists():
        return False, "not a git checkout"
    try:
        p = subprocess.run(["git", "-C", str(checkout), "pull", "--ff-only"],
                           capture_output=True, text=True, timeout=180)
    except (OSError, subprocess.SubprocessError) as e:
        return False, str(e)
    out = (p.stdout or p.stderr or "").strip().splitlines()
    return p.returncode == 0, out[-1] if out else ""


def run(explicit: str | None = None, pull: bool = True) -> Outcome:
    before = version()
    source, _how = resolve_source(explicit)

    if not is_virtualenv():
        return Outcome(
            False, before, before, source,
            "CodeOrbit is not in its own virtualenv, so upgrading it here would "
            "modify the system Python. Use: pip install --upgrade codeorbit",
        )

    note = ""
    as_path = Path(source)
    if as_path.exists() and pull:
        ok, msg = git_pull(as_path)
        note = f"git pull: {msg}" if ok else f"git pull skipped ({msg})"

    # Check BEFORE running pip, never after. `pip install --upgrade` uninstalls
    # the old version first, so a failure half-way leaves the venv with no
    # working codeorbit at all - which is exactly what happened when this was
    # written as try-then-recover. If the upgrade cannot succeed in-process,
    # it must not be started in-process.
    if launched_via_console_script():
        log = _spawn_detached_upgrade(source)
        return Outcome(
            True, before, "pending", source,
            f"finishing after this process exits (log: {log})"
            if log else "finishing after this process exits",
        )

    code, out = _pip("install", "--upgrade", source)

    if code != 0 and _is_self_lock(out):
        log = _spawn_detached_upgrade(source)
        return Outcome(
            True, before, "pending", source,
            f"finishing after this process exits (log: {log})"
            if log else "finishing after this process exits",
        )

    if code != 0:
        tail = " | ".join(l.strip() for l in out.strip().splitlines()[-3:])
        return Outcome(False, before, before, source, tail[:400] or "pip failed")

    record_source(source)

    # The running process still holds the OLD version in memory, so ask a fresh
    # interpreter what actually landed rather than reporting our own stale value.
    try:
        p = subprocess.run(
            [sys.executable, "-c",
             "import importlib.metadata as m; print(m.version('codeorbit'))"],
            capture_output=True, text=True, timeout=60)
        after = p.stdout.strip() or before
    except Exception:
        after = before

    return Outcome(True, before, after, source, note)
