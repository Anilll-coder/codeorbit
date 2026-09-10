"""Generate site/commands.json from the CLI itself.

Hand-written command documentation is wrong within two releases. Every flag
here is read out of the live Click command objects, so the site cannot claim an
option that does not exist or miss one that does. Only the examples are written
by hand, and a test asserts every command has one.

    python scripts/gen_commands.py          # rewrite site/commands.json
    python scripts/gen_commands.py --check  # fail if it is out of date (CI)
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import click
import typer.main

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from codeorbit.cli import app  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "site" / "commands.json"

# What someone would actually type. One line per example, with the reason it is
# worth showing - the site renders the note under the command.
EXAMPLES: dict[str, list[tuple[str, str]]] = {
    "index": [
        ("codeorbit index .", "Build the graph for the current project."),
        ("codeorbit index ~/code/app", "Index somewhere else."),
        ("codeorbit index --full", "Re-parse every file, ignoring the hash cache."),
    ],
    "status": [
        ("codeorbit status", "What the index holds, and whether Ollama is up."),
    ],
    "embed": [
        ("codeorbit embed", "One time. Lets `ask` find code by meaning, not just names."),
    ],
    "search": [
        ("codeorbit search Console", "Find symbols whose name matches."),
        ("codeorbit search render --limit 30", "More results."),
    ],
    "show": [
        ("codeorbit show Console.print", "Source, callers and callees in one view."),
    ],
    "callers": [
        ("codeorbit callers render", "Who calls this."),
    ],
    "impact": [
        ("codeorbit impact Segment", "Everything that transitively reaches this symbol."),
        ("codeorbit impact Segment --depth 2", "Stop at two hops."),
    ],
    "entry": [
        ("codeorbit entry", "What the most code depends on: where to start reading."),
    ],
    "dead": [
        ("codeorbit dead", "Definitions nothing in the project calls."),
    ],
    "why": [
        ("codeorbit why make_user normalize", "The call path between two symbols."),
    ],
    "viz": [
        ("codeorbit viz", "Explore the graph in a browser, served from your machine."),
        ("codeorbit viz --focus Segment", "Open centred on one symbol."),
        ("codeorbit viz --out graph.html", "Write a standalone file instead of serving."),
        ("codeorbit viz --format mermaid", "A diagram to paste into a report."),
    ],
    "ask": [
        ('codeorbit ask "How does Console.print render output?"',
         "Answered by a local model, grounded in the graph."),
        ('codeorbit ask "what calls load?" --show-context',
         "See exactly what the model was given."),
    ],
    "agent": [
        ('codeorbit agent "is load safe to change?"',
         "A local model drives the MCP tools itself, over several steps."),
    ],
    "review": [
        ("codeorbit review", "Review your working tree with its blast radius."),
        ("codeorbit review --base main", "Review the branch against main."),
        ("codeorbit review --staged --no-llm", "Just the risk table, instantly."),
        ("codeorbit review --pr 42", "Review a GitHub PR against a graph of its own head."),
        ("codeorbit review --pr 42 --post", "...and offer to post it back. Asks first."),
    ],
    "audit": [
        ("codeorbit audit", "Bugs and security issues, ranked by how much they reach."),
        ("codeorbit audit -s high --explain", "High severity only, explained by the model."),
    ],
    "fix": [
        ("codeorbit fix -s high", "Propose fixes. Preview only, nothing is written."),
        ("codeorbit fix -s high --apply --test", "Write them, gated on your test suite."),
    ],
    "mcp": [
        ("codeorbit mcp", "Serve the graph over MCP. Agents run this, you rarely do."),
    ],
    "install-mcp": [
        ("codeorbit install-mcp", "Register with Claude Code."),
        ("codeorbit install-mcp -a cursor", "...or Cursor, or windsurf."),
        ("codeorbit install-mcp --print", "Show the config without writing it."),
    ],
    "upgrade": [
        ("codeorbit upgrade", "Upgrade in place from wherever it was installed."),
        ("codeorbit upgrade --dry-run", "Show what would happen."),
    ],
    "uninstall": [
        ("codeorbit uninstall --dry-run", "Show exactly what removal would touch."),
        ("codeorbit uninstall", "Remove it. Project indexes are kept."),
    ],
}


def param_kind(p: click.Parameter) -> str:
    """option or argument, without isinstance.

    Typer's TyperOption does not subclass click.Option in every Typer/Click
    combination - here its MRO is Parameter only - so an isinstance check
    silently discards every flag and the generated page documents a command as
    having no options at all. `param_type_name` is Click's own discriminator
    and is stable across both.
    """
    kind = getattr(p, "param_type_name", "")
    if kind in ("option", "argument"):
        return kind
    return "option" if any(o.startswith("-") for o in getattr(p, "opts", [])) else "argument"


def describe_param(p: click.Parameter) -> dict | None:
    if param_kind(p) == "argument":
        return {
            "kind": "argument",
            "name": (p.metavar or p.name or "").strip(),
            "help": (getattr(p, "help", "") or "").strip(),
            "required": bool(p.required),
        }
    if p.name == "help":
        return None
    default = p.default
    if default in (None, False):
        default = ""
    elif default is True:
        default = "on"
    else:
        default = str(default)
    return {
        "kind": "option",
        "name": ", ".join(p.opts + p.secondary_opts),
        "help": (p.help or "").strip(),
        "required": bool(p.required),
        "default": default,
        "metavar": (p.metavar or "").strip(),
    }


def build() -> dict:
    cli = typer.main.get_command(app)
    ctx = click.Context(cli, info_name="codeorbit")

    commands = []
    for name in sorted(cli.list_commands(ctx)):
        cmd = cli.get_command(ctx, name)
        if cmd is None or cmd.hidden:
            continue

        doc = (cmd.help or "").strip()
        summary = (cmd.short_help or doc.split("\n")[0]).strip()
        # Anything after the first line is the longer explanation.
        rest = "\n".join(doc.split("\n")[1:]).strip()

        params = [describe_param(p) for p in cmd.params]
        params = [p for p in params if p]

        sub = click.Context(cmd, info_name=name, parent=ctx)
        commands.append({
            "name": name,
            "panel": getattr(cmd, "rich_help_panel", "") or "Other",
            "summary": summary,
            "description": rest,
            "usage": " ".join(cmd.collect_usage_pieces(sub)),
            "arguments": [p for p in params if p["kind"] == "argument"],
            "options": [p for p in params if p["kind"] == "option"],
            "examples": [{"cmd": c, "note": n}
                         for c, n in EXAMPLES.get(name, [])],
        })

    # Panel order comes from the CLI's own ordering, which is the order the
    # commands are defined in, so the site groups them the way --help does.
    seen: list[str] = []
    for c in sorted(commands, key=lambda c: c["name"]):
        if c["panel"] not in seen:
            seen.append(c["panel"])

    return {
        "generated_by": "scripts/gen_commands.py",
        "panels": PANEL_ORDER + [p for p in seen if p not in PANEL_ORDER],
        "commands": commands,
    }


PANEL_ORDER = [
    "Build the graph", "Explore", "Ask questions",
    "Review and fix", "Connect an AI agent", "Manage this install",
]


def main() -> int:
    data = build()
    text = json.dumps(data, indent=2) + "\n"

    if "--check" in sys.argv:
        current = OUT.read_text(encoding="utf-8") if OUT.exists() else ""
        if current != text:
            print(f"{OUT} is out of date. Run: python scripts/gen_commands.py")
            return 1
        print(f"{OUT} is up to date.")
        return 0

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(text, encoding="utf-8")
    print(f"wrote {OUT} ({len(data['commands'])} commands)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
