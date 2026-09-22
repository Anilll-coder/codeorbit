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


@pytest.fixture(scope="module")
def languages():
    return gen_commands.build_languages()


@pytest.fixture(scope="module")
def committed_languages():
    path = ROOT / "site" / "languages.json"
    assert path.is_file(), "site/languages.json is missing. Run: python scripts/gen_commands.py"
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def site_html():
    return (ROOT / "site" / "index.html").read_text(encoding="utf-8")


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
    for name in ("commands.json", "commands.js", "languages.json", "languages.js",
                 "index.html", "app.js", "styles.css"):
        assert (ROOT / "site" / name).is_file(), f"site/{name} is missing"
    html = (ROOT / "site" / "index.html").read_text(encoding="utf-8")
    assert 'id="usage"' in html
    assert "commands.js" in html
    assert 'id="languages"' in html
    assert "languages.js" in html


# ----------------------------------------------------------------- languages
#
# The coverage section's numbers ARE its claim. A page advertising 137
# languages beside a build that reads three would be worse than a page saying
# nothing, so the same discipline applies here as to the command reference:
# generated from the registry, and the committed copy is asserted to match.

def test_the_committed_language_reference_matches_the_registry(
        languages, committed_languages):
    assert committed_languages == languages, (
        "site/languages.json is out of date with the language registry.\n"
        "Run: python scripts/gen_commands.py")


def test_every_language_listed_is_one_the_tool_can_parse(languages):
    """The page must never name a language this build has no grammar for."""
    from codeorbit import langs

    supported = langs.supported()
    for entry in languages["languages"]:
        assert entry["id"] in supported, f"{entry['id']} is listed but unparseable"


def test_the_tiers_partition_the_languages(languages):
    """Every language is in exactly one tier, so the three counts on the page
    add up to the headline count. Three numbers that do not sum to the fourth
    is the kind of detail a reader checks."""
    tiers = languages["tiers"]
    assert (len(tiers["builtin"]) + len(tiers["tabled"]) + tiers["inferred"]
            == languages["count"])
    seen = [e["tier"] for e in languages["languages"]]
    assert set(seen) <= {"builtin", "tabled", "inferred"}


def test_the_page_fallback_numbers_match_the_generated_ones(languages, site_html):
    """The markup carries static numbers so the section is right with no JS.
    Static means they can drift, which is exactly what this catches."""
    import re

    def fallback(attr):
        m = re.search(rf'{attr}[^>]*>([\d,]+)<', site_html)
        assert m, f"no static fallback for {attr} in index.html"
        return int(m.group(1).replace(",", ""))

    assert fallback("data-lang-count") == languages["count"]
    assert fallback("data-suffix-count") == languages["suffixes"]
    assert fallback('data-tier-count="builtin"') == len(languages["tiers"]["builtin"])
    assert fallback('data-tier-count="tabled"') == len(languages["tiers"]["tabled"])
    assert fallback('data-tier-count="inferred"') == languages["tiers"]["inferred"]


def test_the_readme_badges_and_tiers_match_the_registry(languages):
    """The README leads with the numbers, in a badge and again in a table.

    A badge is the first thing a reader believes and the last thing anyone
    remembers to update, so it gets the same treatment as the site: asserted
    against what the tool actually supports.
    """
    import re

    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    badge = re.search(r"badge/languages-(\d+)-", readme)
    assert badge, "no languages badge in README.md"
    assert int(badge.group(1)) == languages["count"]

    ext = re.search(r"badge/extensions-(\d+)-", readme)
    assert ext, "no extensions badge in README.md"
    assert int(ext.group(1)) == languages["suffixes"]

    # The tier table: "| **1** | Hand-written | **4** | ..."
    tiers = [int(n) for n in re.findall(r"\|\s*\*\*[123]\*\*\s*\|[^|]+\|\s*\*\*(\d+)\*\*\s*\|",
                                        readme)]
    assert tiers == [
        len(languages["tiers"]["builtin"]),
        len(languages["tiers"]["tabled"]),
        languages["tiers"]["inferred"],
    ], f"README tier counts {tiers} disagree with the registry"


def test_the_readme_logo_exists():
    """A broken image at the top of a README is the worst possible first
    impression, and nothing else in the repo references this file."""
    assert (ROOT / "assets" / "logo.svg").is_file()
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "assets/logo.svg" in readme


def test_every_static_language_count_on_the_page_agrees(languages, site_html):
    """The count appears in the hero, the stat band and the footer. One of
    them going stale is how a page starts contradicting itself."""
    import re

    counts = {int(n) for n in re.findall(r'data-lang-count[^>]*>(\d+)<', site_html)}
    assert counts == {languages["count"]}, counts
