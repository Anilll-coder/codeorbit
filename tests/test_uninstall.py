"""Tests for the uninstall command.

This is the one command that deletes things it did not create, and it is asked
to delete the environment it is running from. So what is pinned here is
restraint: that it refuses outside a virtualenv, that it never proposes to
remove a source checkout, and that project indexes survive unless explicitly
requested.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from codeorbit import cli, uninstall
from codeorbit.db import DB_DIRNAME

runner = CliRunner()


@pytest.fixture(autouse=True)
def _reset_shared_path():
    cli._shared["path"] = None
    yield
    cli._shared["path"] = None


def fake_venv(root: Path) -> Path:
    """A directory shaped enough like a virtualenv to be recognised."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "pyvenv.cfg").write_text("home = /usr\n", encoding="utf-8")
    binp = root / ("Scripts" if sys.platform == "win32" else "bin")
    binp.mkdir(parents=True, exist_ok=True)
    return root


# ------------------------------------------------------------------ guards

def test_refuses_outside_a_virtualenv(tmp_path: Path, monkeypatch):
    """The critical guard: a system Python must never be removed."""
    monkeypatch.setattr(sys, "prefix", str(tmp_path))   # no pyvenv.cfg
    plan = uninstall.build_plan()
    assert plan.refusal
    assert "pip uninstall" in plan.refusal
    assert plan.venv is None
    assert not plan.anything


def test_cli_exits_nonzero_when_it_refuses(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(sys, "prefix", str(tmp_path))
    r = runner.invoke(cli.app, ["uninstall", "--yes"])
    assert r.exit_code == 1
    assert "refuses" in r.output or "pip uninstall" in r.output


def test_plans_to_remove_a_real_virtualenv(tmp_path: Path, monkeypatch):
    venv = fake_venv(tmp_path / "env")
    monkeypatch.setattr(sys, "prefix", str(venv))
    monkeypatch.setattr(uninstall, "_launchers", lambda venv=None: [])
    plan = uninstall.build_plan()
    assert plan.refusal is None
    assert plan.venv == venv.resolve()


# ------------------------------------------------------------------ indexes

def test_index_is_kept_by_default(tmp_path: Path, monkeypatch):
    venv = fake_venv(tmp_path / "env")
    monkeypatch.setattr(sys, "prefix", str(venv))
    monkeypatch.setattr(uninstall, "_launchers", lambda venv=None: [])
    project = tmp_path / "proj"
    (project / DB_DIRNAME).mkdir(parents=True)

    plan = uninstall.build_plan(project, with_index=False)
    assert plan.index is None


def test_index_is_removed_only_when_asked(tmp_path: Path, monkeypatch):
    venv = fake_venv(tmp_path / "env")
    monkeypatch.setattr(sys, "prefix", str(venv))
    monkeypatch.setattr(uninstall, "_launchers", lambda venv=None: [])
    project = tmp_path / "proj"
    idx = project / DB_DIRNAME
    idx.mkdir(parents=True)
    (idx / "graph.db").write_text("x", encoding="utf-8")

    plan = uninstall.build_plan(project, with_index=True)
    assert plan.index == idx
    uninstall.execute(plan)
    assert not idx.exists()


# ------------------------------------------------------------------ launchers

def test_launchers_are_removed(tmp_path: Path, monkeypatch):
    venv = fake_venv(tmp_path / "env")
    launcher = tmp_path / "bin" / "codeorbit"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("#!/bin/sh\n", encoding="utf-8")

    monkeypatch.setattr(sys, "prefix", str(venv))
    monkeypatch.setattr(uninstall, "_launchers", lambda venv=None: [launcher])

    plan = uninstall.build_plan()
    assert launcher in plan.launchers
    uninstall.execute(plan)
    assert not launcher.exists()


def test_only_launchers_pointing_at_this_venv_are_removed(tmp_path: Path, monkeypatch):
    """Found for real: uninstalling one install deleted another one's launcher.

    Two installs can coexist - a throwaway under a temp dir and a real one under
    LOCALAPPDATA. The launcher search collected every shim in every known
    location, so removing the throwaway also removed the real install's
    launcher and its PATH entry. A shim is only ours if it names our venv.
    """
    mine = fake_venv(tmp_path / "mine")
    theirs = fake_venv(tmp_path / "theirs")

    binp = tmp_path / "bin"
    binp.mkdir()
    my_shim = binp / "codeorbit"
    my_shim.write_text(f'exec "{mine}/Scripts/codeorbit.exe" "$@"\n', encoding="utf-8")

    other_bin = tmp_path / "otherbin"
    other_bin.mkdir()
    their_shim = other_bin / "codeorbit.cmd"
    their_shim.write_text(f'@echo off\n"{theirs}\\Scripts\\codeorbit.exe" %*\n',
                          encoding="utf-8")

    monkeypatch.setenv("CODEORBIT_BIN", str(binp))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "nothing-here"))
    monkeypatch.setattr(sys, "prefix", str(mine))

    plan = uninstall.build_plan()
    assert my_shim in plan.launchers
    assert their_shim not in plan.launchers, "another install's launcher must survive"

    uninstall.execute(plan)
    assert not my_shim.exists()
    assert their_shim.exists(), "another install's launcher was deleted"


def test_no_path_entry_is_surrendered_when_no_launcher_is_ours(tmp_path: Path,
                                                               monkeypatch):
    """Without a shim of ours in it, that directory is not ours to unset."""
    mine = fake_venv(tmp_path / "mine")
    other_bin = tmp_path / "bin"
    other_bin.mkdir()
    (other_bin / "codeorbit.cmd").write_text(
        '@echo off\n"C:\\somewhere\\else\\codeorbit.exe" %*\n', encoding="utf-8")

    monkeypatch.setenv("CODEORBIT_BIN", str(other_bin))
    monkeypatch.setattr(sys, "prefix", str(mine))

    plan = uninstall.build_plan()
    assert plan.launchers == []
    assert plan.path_entry is None


# ------------------------------------------------------------------ dry run

def test_dry_run_removes_nothing(tmp_path: Path, monkeypatch):
    venv = fake_venv(tmp_path / "env")
    launcher = tmp_path / "bin" / "codeorbit"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("#!/bin/sh\n", encoding="utf-8")

    monkeypatch.setattr(sys, "prefix", str(venv))
    monkeypatch.setattr(uninstall, "_launchers", lambda venv=None: [launcher])

    r = runner.invoke(cli.app, ["uninstall", "--dry-run"])
    assert r.exit_code == 0
    assert "nothing was removed" in r.output.lower()
    assert launcher.exists()
    assert venv.exists()


def test_declining_the_prompt_removes_nothing(tmp_path: Path, monkeypatch):
    venv = fake_venv(tmp_path / "env")
    monkeypatch.setattr(sys, "prefix", str(venv))
    monkeypatch.setattr(uninstall, "_launchers", lambda venv=None: [])

    r = runner.invoke(cli.app, ["uninstall"], input="n\n")
    assert r.exit_code == 1
    assert "cancelled" in r.output.lower()
    assert venv.exists()


def test_it_says_what_it_will_not_touch(tmp_path: Path, monkeypatch):
    venv = fake_venv(tmp_path / "env")
    monkeypatch.setattr(sys, "prefix", str(venv))
    monkeypatch.setattr(uninstall, "_launchers", lambda venv=None: [])
    r = runner.invoke(cli.app, ["uninstall", "--dry-run"])
    assert "NOT touch" in r.output
    assert "source code" in r.output


# ------------------------------------------------------------- editable check

def test_editable_source_is_reported_and_never_deleted(tmp_path: Path, monkeypatch):
    """A dev checkout must be named as protected, and never end up in the plan."""
    venv = fake_venv(tmp_path / "env")
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / "pyproject.toml").write_text("[project]\n", encoding="utf-8")

    monkeypatch.setattr(sys, "prefix", str(venv))
    monkeypatch.setattr(uninstall, "_launchers", lambda venv=None: [])
    monkeypatch.setattr(uninstall, "_editable_source", lambda: checkout)

    plan = uninstall.build_plan()
    assert plan.editable_source == checkout
    # The checkout must never be a deletion target.
    assert plan.venv != checkout
    assert checkout not in plan.launchers
    uninstall.execute(plan)
    assert checkout.exists() and (checkout / "pyproject.toml").exists()
