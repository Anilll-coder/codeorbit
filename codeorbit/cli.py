"""CodeOrbit CLI - local-first code intelligence with a local LLM."""
from __future__ import annotations

import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table

from . import context as ctxmod
from . import db, llm, query
from . import review as reviewmod
from .indexer import index_project
from .resolve import resolve_project

app = typer.Typer(add_completion=False, help="Local code intelligence over a code graph.")
console = Console()


def _open(path: str):
    root = Path(path).resolve()
    try:
        return root, query.open_graph(root)
    except FileNotFoundError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)


def _pick(conn, name: str):
    hits = query.search(conn, name, limit=10)
    if not hits:
        console.print(f"[red]No symbol matching[/red] {name!r}")
        raise typer.Exit(1)
    return hits[0]


@app.command()
def index(path: str = typer.Argument(".", help="Project root to index")):
    """Parse the project and build its graph."""
    root = Path(path).resolve()
    console.print(f"Indexing [bold]{root}[/bold]")
    with console.status("parsing..."):
        st = index_project(root)
    console.print(
        f"  parsed [bold]{st['files']}[/bold] files "
        f"([bold]{st['loc']:,}[/bold] lines) -> "
        f"[bold]{st['nodes']}[/bold] symbols"
    )
    with console.status("resolving references..."):
        rs = resolve_project(root)
    total = rs["exact"] + rs["heuristic"] + rs["unresolved"]
    pct = 100 * (rs["exact"] + rs["heuristic"]) / max(total, 1)
    console.print(
        f"  resolved [bold]{rs['exact']}[/bold] exact + "
        f"[bold]{rs['heuristic']}[/bold] heuristic call edges "
        f"([bold]{pct:.0f}%[/bold] of in-project call sites)"
    )
    console.print(f"  graph: [bold]{rs['edges']}[/bold] edges")
    console.print(f"[green]Done.[/green] Index at {db.db_path(root)}")


@app.command()
def status(path: str = typer.Argument(".")):
    """Show what the index contains."""
    root, conn = _open(path)
    st = db.stats(conn)
    t = Table(show_header=False, box=None)
    t.add_row("project", str(root))
    for k in ("files", "loc", "nodes", "edges"):
        t.add_row(k, f"{st[k]:,}")
    kinds = conn.execute(
        "SELECT kind, count(*) c FROM nodes GROUP BY kind ORDER BY c DESC"
    ).fetchall()
    t.add_row("symbols", ", ".join(f"{r['kind']}={r['c']}" for r in kinds))
    ek = conn.execute(
        "SELECT kind, count(*) c FROM edges GROUP BY kind ORDER BY c DESC"
    ).fetchall()
    t.add_row("edges by kind", ", ".join(f"{r['kind']}={r['c']}" for r in ek))
    t.add_row("ollama", "up" if llm.available() else "[red]not running[/red]")
    console.print(Panel(t, title="CodeOrbit"))


@app.command()
def search(term: str, path: str = typer.Option(".", "--path", "-p"), limit: int = 15):
    """Find symbols by name."""
    root, conn = _open(path)
    rows = query.search(conn, term, limit)
    if not rows:
        console.print("[yellow]no matches[/yellow]")
        return
    t = Table("kind", "symbol", "location")
    for r in rows:
        t.add_row(r["kind"], r["qname"], f"{r['path']}:{r['start_line']}")
    console.print(t)


@app.command()
def show(name: str, path: str = typer.Option(".", "--path", "-p")):
    """Show a symbol's source with its callers and callees."""
    root, conn = _open(path)
    row = _pick(conn, name)
    console.print(
        f"[bold]{row['kind']} {row['qname']}[/bold]  "
        f"[dim]{row['path']}:{row['start_line']}-{row['end_line']}[/dim]"
    )
    ins = query.callers(conn, row["id"])
    outs = query.callees(conn, row["id"])
    if ins:
        console.print("\n[cyan]called by[/cyan]")
        for c in ins:
            mark = "" if c["resolution"] == "exact" else " [dim](uncertain)[/dim]"
            console.print(f"  {c['qname']}  [dim]{c['path']}:{c['call_line']}[/dim]{mark}")
    if outs:
        console.print("\n[cyan]calls[/cyan]")
        for c in outs:
            mark = "" if c["resolution"] == "exact" else " [dim](uncertain)[/dim]"
            console.print(f"  {c['qname']}  [dim]line {c['call_line']}[/dim]{mark}")
    body = query.source_of(root, row, max_lines=120)
    if body:
        console.print()
        console.print(Syntax(body, row["lang"], theme="ansi_dark", word_wrap=False))


@app.command()
def callers(name: str, path: str = typer.Option(".", "--path", "-p")):
    """Who calls this symbol."""
    root, conn = _open(path)
    row = _pick(conn, name)
    rows = query.callers(conn, row["id"], limit=50)
    console.print(f"[bold]{len(rows)}[/bold] callers of {row['qname']}")
    for c in rows:
        console.print(f"  {c['qname']}  [dim]{c['path']}:{c['call_line']}[/dim]")


@app.command()
def impact(name: str, path: str = typer.Option(".", "--path", "-p"), depth: int = 3):
    """Blast radius: everything that transitively reaches this symbol."""
    root, conn = _open(path)
    row = _pick(conn, name)
    hits = query.impact(conn, row["id"], depth)
    console.print(
        f"Changing [bold]{row['qname']}[/bold] can affect "
        f"[bold]{len(hits)}[/bold] symbols (depth {depth})"
    )
    for d, r in hits[:40]:
        console.print(f"  {'  ' * (d - 1)}[dim]{d}[/dim] {r['qname']}  [dim]{r['path']}[/dim]")
    tests = query.affected_tests(conn, row["id"], depth + 1)
    if tests:
        console.print(f"\n[yellow]tests to run ({len(tests)})[/yellow]")
        for t in tests[:20]:
            console.print(f"  {t['qname']}  [dim]{t['path']}[/dim]")


@app.command()
def entry(path: str = typer.Option(".", "--path", "-p")):
    """What the most code depends on - a place to start reading."""
    root, conn = _open(path)
    t = Table("fan-in", "symbol", "location")
    for r in query.entry_points(conn):
        t.add_row(str(r["fan_in"]), r["qname"], f"{r['path']}:{r['start_line']}")
    console.print(t)


@app.command()
def dead(path: str = typer.Option(".", "--path", "-p")):
    """Definitions nothing in the project calls."""
    root, conn = _open(path)
    rows = query.dead_code(conn)
    console.print(f"[bold]{len(rows)}[/bold] uncalled definitions")
    for r in rows:
        span = r["end_line"] - r["start_line"] + 1
        console.print(f"  {r['qname']}  [dim]{r['path']}:{r['start_line']} ({span} lines)[/dim]")


@app.command()
def ask(
    question: str,
    path: str = typer.Option(".", "--path", "-p"),
    model: str = typer.Option(llm.DEFAULT_MODEL, "--model", "-m"),
    symbols: int = typer.Option(2, "--symbols", "-n", help="How many symbols to retrieve"),
    max_tokens: int = typer.Option(320, "--max-tokens", "-t",
                                   help="Cap the answer length (CPU generates ~1-2 tok/s)"),
    show_context: bool = typer.Option(False, "--show-context", help="Print what was retrieved"),
    no_llm: bool = typer.Option(False, "--no-llm", help="Retrieve only, skip the model"),
):
    """Ask a question about the codebase, answered from the graph by a local model."""
    root, conn = _open(path)

    with console.status("retrieving from graph..."):
        ctx, picks = ctxmod.build(root, conn, question, max_symbols=symbols)

    if not picks:
        console.print("[yellow]Nothing in the graph matched that question.[/yellow]")
        console.print("Try naming a symbol, or run [bold]codeorbit search <name>[/bold].")
        raise typer.Exit(1)

    console.print("[dim]retrieved: " + ", ".join(
        f"{p['qname']} ({p['path']}:{p['start_line']})" for p in picks
    ) + f"  |  {len(ctx):,} chars[/dim]\n")

    if show_context:
        console.print(Panel(ctx[:6000], title="context"))

    if no_llm:
        return

    if not llm.available():
        console.print("[red]Ollama is not running.[/red] Start it with: ollama serve")
        raise typer.Exit(1)

    have = llm.models()
    if model not in have and f"{model}:latest" not in have:
        console.print(f"[red]Model {model!r} not found.[/red] Installed: {', '.join(have)}")
        raise typer.Exit(1)

    prompt = ctxmod.prompt_for(question, ctx)
    try:
        for piece in llm.stream(prompt, model=model, system=ctxmod.SYSTEM,
                                num_predict=max_tokens):
            sys.stdout.write(piece)
            sys.stdout.flush()
    except llm.OllamaError as e:
        console.print(f"\n[red]{e}[/red]")
        raise typer.Exit(1)
    print()


@app.command()
def review(
    path: str = typer.Option(".", "--path", "-p"),
    base: str = typer.Option(None, "--base", "-b",
                             help="Review against this ref (e.g. main, HEAD~1)"),
    staged: bool = typer.Option(False, "--staged", help="Review staged changes only"),
    model: str = typer.Option(llm.DEFAULT_MODEL, "--model", "-m"),
    symbols: int = typer.Option(4, "--symbols", "-n", help="Max changed symbols to review"),
    max_tokens: int = typer.Option(400, "--max-tokens", "-t"),
    show_context: bool = typer.Option(False, "--show-context"),
    no_llm: bool = typer.Option(False, "--no-llm", help="Show the blast radius, skip the model"),
):
    """Review the current change together with what the graph says it reaches."""
    root, conn = _open(path)

    if not reviewmod.is_repo(root):
        console.print(f"[red]{root} is not a git repository.[/red]")
        raise typer.Exit(1)

    try:
        with console.status("reading diff and walking the graph..."):
            diff, changed, summary = reviewmod.collect(root, conn, base, staged, symbols)
    except reviewmod.GitError as e:
        console.print(f"[red]git: {e}[/red]")
        raise typer.Exit(1)

    if not diff.strip():
        where = "staged" if staged else (f"vs {base}" if base else "working tree")
        console.print(f"[yellow]No changes to review ({where}).[/yellow]")
        raise typer.Exit(0)

    console.print(
        f"[bold]{summary['files']}[/bold] file(s) changed  "
        f"[green]+{summary['added']}[/green] [red]-{summary['removed']}[/red]  |  "
        f"[bold]{summary['symbols']}[/bold] symbol(s) touched"
    )

    if changed:
        t = Table("symbol", "blast", "callers", "tests", title="what the change reaches")
        for cs in changed:
            t.add_row(
                cs.row["qname"],
                str(cs.blast),
                str(len(cs.callers)),
                str(len(cs.tests)) if cs.tests else "[red]none[/red]",
            )
        console.print(t)

    if summary["unindexed"]:
        console.print(
            "[yellow]not in the index[/yellow] (nothing known about what they reach): "
            + ", ".join(summary["unindexed"][:6])
        )

    prompt = reviewmod.build_prompt(root, diff, changed, summary)
    if show_context:
        console.print(Panel(prompt[:6000], title="review context"))
    if no_llm:
        return

    if not llm.available():
        console.print("[red]Ollama is not running.[/red] Start it with: ollama serve")
        raise typer.Exit(1)

    console.print()
    try:
        for piece in llm.stream(prompt, model=model, system=reviewmod.SYSTEM,
                                num_predict=max_tokens):
            sys.stdout.write(piece)
            sys.stdout.flush()
    except llm.OllamaError as e:
        console.print(f"\n[red]{e}[/red]")
        raise typer.Exit(1)
    print()


def main():
    app()


if __name__ == "__main__":
    main()
