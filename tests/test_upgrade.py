"""`codeorbit upgrade` must install the new code even when the version number is
unchanged - every build so far is 0.1.0, and `pip install --upgrade` alone kept
the old code while reporting success."""
from __future__ import annotations

from codeorbit import upgrade


def test_it_reinstalls_the_package_whatever_its_version(monkeypatch):
    calls = []
    monkeypatch.setattr(upgrade, "is_virtualenv", lambda: True)
    monkeypatch.setattr(upgrade, "launched_via_console_script", lambda: False)
    monkeypatch.setattr(upgrade, "resolve_source",
                        lambda explicit=None: ("git+https://example.invalid/x.git@main", "given"))
    monkeypatch.setattr(upgrade, "_pip", lambda *a, **k: (calls.append(a), (0, ""))[1])
    monkeypatch.setattr(upgrade, "record_source", lambda s: None)
    upgrade.run("git+https://example.invalid/x.git@main", pull=False)
    assert calls[0][:4] == ("install", "--upgrade", "--force-reinstall", "--no-deps")
    assert calls[1] == ("install", "git+https://example.invalid/x.git@main")   # deps


def test_the_detached_upgrade_reinstalls_too():
    import inspect
    src = inspect.getsource(upgrade._spawn_detached_upgrade)
    assert "REINSTALL" in src and "--upgrade '{source}'" not in src
