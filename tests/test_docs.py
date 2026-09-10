"""The site's command reference is generated. These tests keep it honest.

Documentation that is written by hand next to the code it documents is wrong
within two releases, and nobody notices because nothing fails. site/commands.json
is generated from the live Click objects, so the only way it can go stale is if
someone changes the CLI and does not regenerate it. That is what these catch.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import gen_commands  # noqa: E402


@pytest.fixture(scope="module")
def generated():
    return gen_commands.build()


@pytest.fixture(scope="module")
def committed():
    path = ROOT / "site" / "commands.json"
    assert path.is_file(), "site/commands.json is missing. Run: python scripts/gen_commands.py"
    return json.loads(path.read_text(encoding="utf-8"))


def test_the_committed_reference_matches_the_cli(generated, committed):
    """The one test that matters. If this fails the site is lying."""
    assert committed == generated, (
        "site/commands.json is out of date with the CLI.\n"
        "Run: python scripts/gen_commands.py")


def test_every_command_is_documented(generated):
    names = {c["name"] for c in generated["commands"]}
    assert len(names) == len(generated["commands"]), "duplicate command names"
    # A few that must always be there; a rename should be deliberate.
    assert {"index", "ask", "review", "viz", "mcp"} <= names


def test_every_command_has_an_example(generated):
    """A flag list is a reference. An example is what someone actually copies."""
    missing = [c["name"] for c in generated["commands"] if not c["examples"]]
    assert not missing, (
        f"no example for: {', '.join(missing)}. Add one to EXAMPLES in "
        "scripts/gen_commands.py")


def test_every_command_has_a_summary(generated):
    empty = [c["name"] for c in generated["commands"] if not c["summary"].strip()]
    assert not empty, f"no summary (docstring) for: {', '.join(empty)}"


def test_every_command_is_in_a_panel(generated):
    """An untagged command falls into 'Other' and reads as an afterthought in
    both `--help` and the site."""
    orphans = [c["name"] for c in generated["commands"] if c["panel"] == "Other"]
    assert not orphans, (
        f"no rich_help_panel on: {', '.join(orphans)}. Add one in cli.py so it "
        "is grouped in --help and on the site.")
    assert set(c["panel"] for c in generated["commands"]) <= set(generated["panels"])


def test_options_are_actually_captured(generated):
    """Guards the Typer/Click isinstance trap: TyperOption does not always
    subclass click.Option, and when the detection breaks every command
    silently documents zero flags."""
    review = next(c for c in generated["commands"] if c["name"] == "review")
    flags = {o["name"] for o in review["options"]}
    assert any("--pr" in f for f in flags), flags
    assert any("--base" in f for f in flags), flags
    assert any("--staged" in f for f in flags), flags
    total = sum(len(c["options"]) for c in generated["commands"])
    assert total > 50, f"only {total} options across the whole CLI; detection is broken"


def test_examples_only_reference_real_commands(generated):
    names = {c["name"] for c in generated["commands"]}
    for cmd in generated["commands"]:
        for ex in cmd["examples"]:
            parts = ex["cmd"].split()
            assert parts[0] == "codeorbit", ex["cmd"]
            # Skip the global-option form (`codeorbit -p DIR audit`).
            if len(parts) > 1 and not parts[1].startswith("-"):
                assert parts[1] in names, f"{ex['cmd']} names an unknown command"


def test_examples_use_flags_the_command_actually_has(generated):
    """An example with a flag that was renamed is worse than no example."""
    for cmd in generated["commands"]:
        known = set()
        for o in cmd["options"]:
            known.update(part.strip() for part in o["name"].split(","))
        for ex in cmd["examples"]:
            for token in ex["cmd"].split():
                if not token.startswith("-") or token == "-":
                    continue
                flag = token.split("=")[0]
                assert flag in known, (
                    f"{ex['cmd']!r} uses {flag}, which {cmd['name']} does not have")


def test_the_check_mode_reports_success_when_in_sync(capsys):
    argv = sys.argv[:]
    sys.argv = ["gen_commands.py", "--check"]
    try:
        assert gen_commands.main() == 0
    finally:
        sys.argv = argv
    assert "up to date" in capsys.readouterr().out


def test_the_site_ships_the_generated_file_and_its_script():
    for name in ("commands.json", "commands.js", "index.html", "app.js", "styles.css"):
        assert (ROOT / "site" / name).is_file(), f"site/{name} is missing"
    html = (ROOT / "site" / "index.html").read_text(encoding="utf-8")
    assert 'id="usage"' in html
    assert "commands.js" in html
