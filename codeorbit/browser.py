"""Open a URL in the user's browser, on any of the machines this runs on.

`codeorbit viz` called `webbrowser.open(url)` and assumed it worked. On Windows
and macOS it does. Everywhere else it has three separate ways to fail, and all
three were reported as success:

  * **It blocks.** `webbrowser` picks a *console* browser - w3m, links, lynx -
    whenever no graphical one is registered, and `GenericBrowser.open` runs it
    in the foreground and waits for it to exit. `viz` calls this from `on_ready`,
    which runs before `serve_forever()`, so the terminal browser took over the
    terminal and the server that was supposed to answer it never started
    serving. The command appeared to hang with the URL printed above it.

  * **It does nothing.** On a headless box, in a container, over SSH without
    X forwarding, `webbrowser.open` returns False. The caller ignored the
    return value and printed nothing, so the user sat waiting for a window.

  * **It is not wired up.** Under WSL, `webbrowser` looks for a Linux browser
    inside the VM and finds none, while the browser the user actually wants is
    on the Windows side and is reached through `wslview` or `powershell.exe`.

So this module does the launching itself: pick the opener for the platform, run
it *detached* so nothing can block, and return a result that says what actually
happened. The one thing it never does is claim to have opened a window it did
not open, because the honest fallback - "open this URL yourself" - is a fine
outcome and a false one wastes the user's time.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

LAUNCH_TIMEOUT = 5.0     # seconds to wait for the opener to *start*, not finish


@dataclass(frozen=True)
class Result:
    opened: bool
    how: str             # the opener used, or why nothing was tried
    hint: str | None = None   # what to tell the user when opened is False


def is_wsl() -> bool:
    """WSL looks like Linux and behaves like neither."""
    if sys.platform != "linux":
        return False
    if os.environ.get("WSL_DISTRO_NAME") or os.environ.get("WSL_INTEROP"):
        return True
    try:
        return "microsoft" in Path("/proc/version").read_text(errors="ignore").lower()
    except OSError:
        return False


def has_display() -> bool:
    """Whether a graphical session exists to open a window into.

    macOS and Windows always have one. On Linux it is DISPLAY or WAYLAND_DISPLAY
    - except under WSL, where the window opens on the Windows side and neither
    variable says anything useful.
    """
    if sys.platform in ("win32", "darwin"):
        return True
    if is_wsl():
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def _spawn(argv: list[str]) -> bool:
    """Start a detached process and do not wait for it.

    Detached matters twice over: a console browser must not inherit this
    terminal, and the opener must not keep running after CodeOrbit exits with
    its stdout wired to a pipe nobody reads, which is how these hang.
    """
    try:
        kwargs: dict = {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
        }
        if os.name == "posix":
            kwargs["start_new_session"] = True
        else:
            kwargs["creationflags"] = getattr(subprocess, "DETACHED_PROCESS", 0)

        proc = subprocess.Popen(argv, **kwargs)
    except (OSError, ValueError):
        return False

    # A launcher either execs the browser and exits, or stays alive holding it.
    # Both are success. Only an immediate non-zero exit is a failure, and that
    # is the one case worth waiting a moment to catch.
    try:
        code = proc.wait(timeout=0.4)
    except subprocess.TimeoutExpired:
        return True
    return code == 0


def _openers() -> list[tuple[str, list[str]]]:
    """(name, argv prefix) for this platform, best first."""
    if sys.platform == "darwin":
        return [("open", ["open"])]

    if sys.platform == "win32":
        # `start` is a cmd builtin, hence cmd /c. The empty "" is the window
        # title argument - without it cmd treats a quoted URL as the title and
        # opens nothing.
        return [("cmd start", ["cmd", "/c", "start", ""])]

    if is_wsl():
        # wslview is part of wslu and is the right answer when present. The
        # other two reach the Windows shell directly.
        return [
            ("wslview", ["wslview"]),
            ("powershell.exe", ["powershell.exe", "-NoProfile", "-Command", "Start-Process"]),
            ("cmd.exe", ["cmd.exe", "/c", "start", ""]),
        ]

    return [
        ("xdg-open", ["xdg-open"]),
        ("gio open", ["gio", "open"]),
        ("gnome-open", ["gnome-open"]),
        ("kde-open", ["kde-open"]),
        ("wslview", ["wslview"]),
    ]


def open_url(url: str) -> Result:
    """Open `url`, without ever blocking and without ever lying about it."""
    if os.environ.get("CODEORBIT_NO_BROWSER"):
        return Result(False, "disabled",
                      "CODEORBIT_NO_BROWSER is set. Open the URL yourself.")

    if not has_display():
        return Result(
            False, "headless",
            "No graphical session was detected (no DISPLAY or WAYLAND_DISPLAY). "
            "If you are on a remote machine, forward the port and open the URL "
            "locally: ssh -L 7373:127.0.0.1:7373 <host>",
        )

    for name, prefix in _openers():
        exe = prefix[0]
        # On Windows and WSL these live outside PATH resolution rules that
        # shutil.which understands for .exe suffixes, so try anyway if it is a
        # shell builtin wrapper.
        if shutil.which(exe) is None and exe not in ("cmd", "cmd.exe"):
            continue
        if _spawn(prefix + [url]):
            return Result(True, name)

    # Last resort, and only where `webbrowser` is known not to block: it hands
    # Windows and macOS straight to the shell. On Linux it is exactly the code
    # path that picks a console browser and waits for it, which is the failure
    # this module exists to avoid - so there, no fallback.
    if sys.platform in ("win32", "darwin") and _webbrowser_in_thread(url):
        return Result(True, "webbrowser")

    return Result(
        False, "no opener",
        "No way to open a browser was found on this machine. "
        "Copy the URL into one yourself.",
    )


def _webbrowser_in_thread(url: str) -> bool:
    """`webbrowser.open`, fenced so a console browser cannot hang the caller."""
    import threading

    outcome: list[bool] = []

    def run() -> None:
        try:
            import webbrowser
            outcome.append(webbrowser.open(url))
        except Exception:      # noqa: BLE001 - any failure is just "no"
            outcome.append(False)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(LAUNCH_TIMEOUT)
    # No answer inside the timeout counts as failure, not success: the caller
    # then prints the URL, which is the right thing to do either way. The thread
    # is a daemon, so whatever it is waiting on cannot hold the command open.
    return bool(outcome) and outcome[0]


def open_file(path: Path) -> Result:
    """Open a local file. Same rules, but a file:// URL."""
    return open_url(path.resolve().as_uri())
