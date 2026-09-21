"""Opening a browser, on every platform this is installed on.

`codeorbit viz` is the command most likely to be run on a machine that is not
the developer's laptop - a server, a container, a WSL shell - and `viz` used to
call `webbrowser.open` and ignore the answer. The three failures that produced
are what these test: it blocks, it silently does nothing, or it looks in the
wrong place entirely.

The tests never launch a browser. They check which opener would be chosen and
that a failure is reported rather than swallowed, which is the part that was
wrong.
"""
from __future__ import annotations

import subprocess
import sys

import pytest

from codeorbit import browser


# ----------------------------------------------------------- opener choice

def test_macos_uses_open(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    assert [n for n, _ in browser._openers()] == ["open"]


def test_windows_passes_a_window_title_to_start(monkeypatch):
    """`cmd /c start "http://..."` treats the quoted URL as the window title
    and opens nothing. The empty "" is the title."""
    monkeypatch.setattr(sys, "platform", "win32")
    _, argv = browser._openers()[0]
    assert argv == ["cmd", "/c", "start", ""]


def test_linux_tries_the_desktop_openers(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(browser, "is_wsl", lambda: False)
    names = [n for n, _ in browser._openers()]
    assert names[0] == "xdg-open"
    assert "gio open" in names


def test_wsl_reaches_the_windows_side(monkeypatch):
    """The browser the user wants is on the Windows host. A Linux opener
    inside the VM finds nothing, which is why viz appeared to do nothing
    under WSL."""
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(browser, "is_wsl", lambda: True)
    names = [n for n, _ in browser._openers()]
    assert names[0] == "wslview"
    assert any("powershell" in n or "cmd" in n for n in names)


# -------------------------------------------------------------- detection

def test_wsl_is_detected_from_the_environment(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu")
    assert browser.is_wsl()


def test_wsl_detection_is_linux_only(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu")
    assert not browser.is_wsl()


def test_a_headless_linux_box_has_no_display(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(browser, "is_wsl", lambda: False)
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    assert not browser.has_display()


def test_windows_and_macos_always_have_a_display(monkeypatch):
    for platform in ("win32", "darwin"):
        monkeypatch.setattr(sys, "platform", platform)
        assert browser.has_display()


# ------------------------------------------------------------- behaviour

def test_headless_reports_instead_of_pretending(monkeypatch):
    """The old code returned nothing and printed nothing, so the user waited
    for a window that was never coming."""
    monkeypatch.setattr(browser, "has_display", lambda: False)
    result = browser.open_url("http://127.0.0.1:7373/")
    assert not result.opened
    assert result.how == "headless"
    assert "ssh -L" in (result.hint or ""), "say what to do about it"


def test_the_environment_can_turn_it_off(monkeypatch):
    monkeypatch.setenv("CODEORBIT_NO_BROWSER", "1")
    result = browser.open_url("http://127.0.0.1:7373/")
    assert not result.opened and result.how == "disabled"


def test_no_opener_on_the_machine_is_reported(monkeypatch):
    monkeypatch.delenv("CODEORBIT_NO_BROWSER", raising=False)
    monkeypatch.setattr(browser, "has_display", lambda: True)
    monkeypatch.setattr(browser, "_openers", lambda: [("nope", ["nope"])])
    monkeypatch.setattr(browser.shutil, "which", lambda _: None)
    monkeypatch.setattr(browser, "_webbrowser_in_thread", lambda _: False)
    result = browser.open_url("http://127.0.0.1:7373/")
    assert not result.opened and result.how == "no opener"


def test_the_first_working_opener_wins(monkeypatch):
    monkeypatch.delenv("CODEORBIT_NO_BROWSER", raising=False)
    monkeypatch.setattr(browser, "has_display", lambda: True)
    monkeypatch.setattr(browser, "_openers",
                        lambda: [("first", ["first"]), ("second", ["second"])])
    monkeypatch.setattr(browser.shutil, "which", lambda name: "/bin/" + name)
    tried: list[str] = []

    def spawn(argv):
        tried.append(argv[0])
        return argv[0] == "second"

    monkeypatch.setattr(browser, "_spawn", spawn)
    result = browser.open_url("http://127.0.0.1:7373/")
    assert result.opened and result.how == "second"
    assert tried == ["first", "second"]


def test_spawning_never_waits_for_the_browser_to_exit(monkeypatch):
    """The original bug: a console browser ran in the foreground and the
    server that was supposed to answer it never started."""
    seen: dict = {}

    class FakeProc:
        def wait(self, timeout=None):
            seen["timeout"] = timeout
            raise subprocess.TimeoutExpired("x", timeout)

    def fake_popen(argv, **kw):
        seen["kw"] = kw
        return FakeProc()

    monkeypatch.setattr(browser.subprocess, "Popen", fake_popen)
    assert browser._spawn(["x", "url"]) is True
    assert seen["timeout"] is not None and seen["timeout"] < 1
    assert seen["kw"]["stdin"] is subprocess.DEVNULL


def test_a_missing_opener_is_not_an_exception(monkeypatch):
    def boom(argv, **kw):
        raise FileNotFoundError(argv[0])

    monkeypatch.setattr(browser.subprocess, "Popen", boom)
    assert browser._spawn(["missing-opener", "url"]) is False


def test_webbrowser_is_not_the_fallback_on_linux(monkeypatch):
    """It is exactly the code path that picks a console browser and waits."""
    monkeypatch.delenv("CODEORBIT_NO_BROWSER", raising=False)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(browser, "has_display", lambda: True)
    monkeypatch.setattr(browser, "_openers", lambda: [])
    called: list[str] = []
    monkeypatch.setattr(browser, "_webbrowser_in_thread",
                        lambda url: called.append(url) or True)

    result = browser.open_url("http://127.0.0.1:7373/")
    assert not result.opened
    assert called == [], "webbrowser must not be reached on Linux"


def test_a_slow_webbrowser_does_not_hang_the_caller(monkeypatch):
    """A console browser never returns. The caller has to move on anyway."""
    import time

    class Slow:
        @staticmethod
        def open(url):
            time.sleep(10)
            return True

    monkeypatch.setitem(sys.modules, "webbrowser", Slow)
    monkeypatch.setattr(browser, "LAUNCH_TIMEOUT", 0.2)
    started = time.monotonic()
    assert browser._webbrowser_in_thread("http://x/") is False
    assert time.monotonic() - started < 3
