"""CodeOrbit CLI - local-first code intelligence with a local LLM."""
from __future__ import annotations

import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table

from . import audit as auditmod
from . import context as ctxmod
from . import db, llm, query
from . import review as reviewmod
from .indexer import index_project
from .resolve import resolve_project

SEV_COLOR = {"high": "red", "medium": "yellow", "low": "dim"}

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
def index(
    path: str = typer.Argument(".", help="Project root to index"),
    full: bool = typer.Option(False, "--full", help="Re-parse every file, ignoring hashes"),
):
    """Parse the project and build its graph (only changed files, unless --full)."""
    root = Path(path).resolve()
    console.print(f"Indexing [bold]{root}[/bold]")
    with console.status("parsing..."):
        st = index_project(root, full=full)

    if not full and st["unchanged"] and not st["touched"]:
        console.print(
            f"  [green]up to date[/green] - {st['unchanged']} file(s) unchanged, nothing to parse"
        )
    else:
        detail = []
        if st["added"]:
            detail.append(f"{st['added']} new")
        if st["changed"]:
            detail.append(f"{st['changed']} changed")
        if st["removed"]:
            detail.append(f"{st['removed']} removed")
        if st["unchanged"]:
            detail.append(f"{st['unchanged']} unchanged")
        console.print(
            f"  parsed [bold]{st['files']}[/bold] files "
            f"([bold]{st['loc']:,}[/bold] lines) -> "
            f"[bold]{st['nodes']}[/bold] symbols"
            + (f"   [dim]({', '.join(detail)})[/dim]" if detail else "")
        )

    # Resolution is whole-graph: a call site binds against every definition in
    # the project, so it cannot be done per-file. But if no file was touched,
    # nothing it would compute has changed - skip it rather than redo it.
    conn = db.connect(root)
    have_edges = conn.execute("SELECT count(*) FROM edges").fetchone()[0]
    conn.close()

    if not full and st["touched"] == 0 and have_edges:
        console.print(f"  [dim]graph unchanged: {have_edges} edges (resolve skipped)[/dim]")
        console.print(f"[green]Done.[/green] Index at {db.db_path(root)}")
        return

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
    no_semantic: bool = typer.Option(False, "--no-semantic",
                                     help="Keyword retrieval only (for A/B comparison)"),
):
    """Ask a question about the codebase, answered from the graph by a local model."""
    root, conn = _open(path)

    with console.status("retrieving from graph..."):
        ctx, picks = ctxmod.build(root, conn, question, max_symbols=symbols,
                                  semantic=not no_semantic)

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


@app.command()
def audit(
    path: str = typer.Option(".", "--path", "-p"),
    severity: str = typer.Option("low", "--severity", "-s",
                                 help="Minimum severity: high, medium or low"),
    include_tests: bool = typer.Option(False, "--include-tests"),
    limit: int = typer.Option(25, "--limit", "-l", help="Findings to display"),
    explain: bool = typer.Option(False, "--explain",
                                 help="Ask the local model which are real and how to fix them"),
    model: str = typer.Option(llm.DEFAULT_MODEL, "--model", "-m"),
    max_tokens: int = typer.Option(400, "--max-tokens", "-t"),
    findings_to_explain: int = typer.Option(4, "--explain-count"),
):
    """Find bugs and security issues, ranked by how much code they reach."""
    root, conn = _open(path)

    if severity not in ("high", "medium", "low"):
        console.print("[red]--severity must be high, medium or low[/red]")
        raise typer.Exit(1)

    with console.status("scanning..."):
        report = auditmod.audit_project(root, conn, include_tests, severity)

    counts = report.by_severity()
    if not report.findings:
        console.print(
            f"[green]No findings[/green] in {report.files_scanned} files "
            f"({report.lines_scanned:,} lines)."
        )
        return

    console.print(
        f"[bold]{len(report.findings)}[/bold] finding(s) in "
        f"{report.files_scanned} files ({report.lines_scanned:,} lines)   "
        + "  ".join(
            f"[{SEV_COLOR[s]}]{counts.get(s, 0)} {s}[/{SEV_COLOR[s]}]"
            for s in ("high", "medium", "low") if counts.get(s)
        )
    )

    t = Table("sev", "finding", "location", "inside", "reach", "test")
    for f in report.findings[:limit]:
        c = SEV_COLOR[f.rule.severity]
        t.add_row(
            f"[{c}]{f.rule.severity}[/{c}]",
            f.rule.title,
            f"{f.path}:{f.line}",
            (f.symbol or "-").rsplit(".", 2)[-1],
            str(f.blast) if f.blast else "-",
            "yes" if f.tested else "[red]no[/red]",
        )
    console.print(t)

    if len(report.findings) > limit:
        console.print(f"[dim]... and {len(report.findings) - limit} more (raise --limit)[/dim]")

    if not explain:
        console.print("\n[dim]--explain asks the local model which are real and how to fix them[/dim]")
        return

    if not llm.available():
        console.print("[red]Ollama is not running.[/red] Start it with: ollama serve")
        raise typer.Exit(1)

    prompt = auditmod.build_prompt(root, conn, report.findings, findings_to_explain)
    console.print()
    try:
        for piece in llm.stream(prompt, model=model, system=auditmod.SYSTEM,
                                num_predict=max_tokens):
            sys.stdout.write(piece)
            sys.stdout.flush()
    except llm.OllamaError as e:
        console.print(f"\n[red]{e}[/red]")
        raise typer.Exit(1)
    print()


@app.command()
def fix(
    path: str = typer.Option(".", "--path", "-p"),
    rule: str = typer.Option(None, "--rule", "-r", help="Only fix findings from this rule id"),
    severity: str = typer.Option("high", "--severity", "-s"),
    limit: int = typer.Option(3, "--limit", "-l", help="How many findings to attempt"),
    apply_fixes: bool = typer.Option(False, "--apply",
                                     help="Write verified fixes (default: preview only)"),
    with_tests: bool = typer.Option(False, "--test",
                                    help="Also run the test suite against each fix"),
    model: str = typer.Option(llm.DEFAULT_MODEL, "--model", "-m"),
    max_tokens: int = typer.Option(500, "--max-tokens", "-t"),
):
    """Generate fixes for audit findings, verify them, and optionally apply."""
    from . import fixer, verify as verifymod

    root, conn = _open(path)

    if not llm.available():
        console.print("[red]Ollama is not running.[/red] Start it with: ollama serve")
        raise typer.Exit(1)

    with console.status("scanning..."):
        report = auditmod.audit_project(root, conn, min_severity=severity)

    targets = [f for f in report.findings if f.symbol_id is not None]
    if rule:
        targets = [f for f in targets if f.rule.id == rule]
    targets = targets[:limit]

    if not targets:
        console.print("[green]Nothing to fix[/green] at that severity.")
        return

    if with_tests:
        with console.status("checking the test suite is green before we start..."):
            base = verifymod.baseline_tests_pass(root)
        if not base.ok:
            console.print(
                f"[red]Tests already fail before any change[/red] ({base.detail}).\n"
                "Fix that first, or drop --test: otherwise every candidate looks rejected."
            )
            raise typer.Exit(1)
        console.print(f"[dim]baseline tests: {base.detail or 'green'}[/dim]")

    console.print(f"Attempting [bold]{len(targets)}[/bold] fix(es)"
                  + ("  [dim](preview only; --apply writes)[/dim]" if not apply_fixes else ""))

    applied = rejected = skipped = 0

    for i, finding in enumerate(targets, 1):
        console.print(
            f"\n[bold]{i}/{len(targets)}[/bold] [{SEV_COLOR[finding.rule.severity]}]"
            f"{finding.rule.severity}[/{SEV_COLOR[finding.rule.severity]}] "
            f"{finding.rule.title}  [dim]{finding.path}:{finding.line}[/dim]"
        )

        with console.status("asking the model..."):
            cand = fixer.propose(root, conn, finding, model=model, max_tokens=max_tokens)

        if cand is None or cand.replacement is None:
            reason = (cand.error if cand else "could not locate the symbol")
            console.print(f"  [yellow]skipped[/yellow] - {reason}")
            skipped += 1
            continue

        with console.status("verifying in a sandbox..."):
            cand = fixer.verify_candidate(root, cand, with_tests)

        for g in cand.verdict.gates:
            mark = "[green]pass[/green]" if g.ok else "[red]FAIL[/red]"
            extra = f" [dim]{g.detail}[/dim]" if g.detail else ""
            console.print(f"  {g.name:<8} {mark}{extra}")

        if not cand.verdict.ok:
            console.print("  [red]rejected[/red] - not written")
            rejected += 1
            continue

        diff = fixer.diff_lines(cand)
        if diff:
            console.print(Syntax("\n".join(diff), "diff", theme="ansi_dark"))

        if apply_fixes:
            target = fixer.apply(root, cand)
            console.print(f"  [green]applied[/green] -> {target.name} "
                          f"[dim](backup: {target.name}.orig)[/dim]")
            applied += 1
        else:
            console.print("  [green]verified[/green] - rerun with --apply to write it")
            applied += 1

    console.print(
        f"\n[bold]{applied}[/bold] verified"
        + (f", [red]{rejected}[/red] rejected" if rejected else "")
        + (f", [yellow]{skipped}[/yellow] skipped" if skipped else "")
    )
    if applied and apply_fixes:
        console.print("[dim]re-run `codeorbit index` to refresh the graph[/dim]")


@app.command()
def embed(
    path: str = typer.Option(".", "--path", "-p"),
    model: str = typer.Option(llm.EMBED_MODEL, "--model", "-m"),
):
    """Build semantic embeddings so `ask` can find code by meaning, not just names."""
    from . import semantic as sem

    root, conn = _open(path)

    if not llm.available():
        console.print("[red]Ollama is not running.[/red] Start it with: ollama serve")
        raise typer.Exit(1)
    have = llm.models()
    if model not in have and f"{model}:latest" not in have:
        console.print(f"[red]Model {model!r} not found.[/red] Try: ollama pull {model}")
        raise typer.Exit(1)

    todo = len(sem.pending(conn, model))
    if not todo:
        console.print(f"[green]Up to date[/green] - {sem.count(conn, model)} symbols embedded.")
        return

    console.print(f"Embedding [bold]{todo}[/bold] symbol(s) with [bold]{model}[/bold]")
    with console.status("embedding...") as status:
        def tick(done, total):
            status.update(f"embedding... {done}/{total}")
        st = sem.build(conn, root, model, progress=tick)

    console.print(f"  [green]{st['embedded']}[/green] embedded"
                  + (f", [red]{st['failed']}[/red] failed" if st["failed"] else ""))
    console.print(f"  {st['total']} symbols now searchable by meaning")
    conn.close()


@app.command()
def why(
    source: str,
    target: str,
    path: str = typer.Option(".", "--path", "-p"),
    max_depth: int = typer.Option(8, "--depth", "-d"),
):
    """Show the call path from one symbol to another."""
    root, conn = _open(path)
    a = _pick(conn, source)
    b = _pick(conn, target)

    hops = query.call_path(conn, a["id"], b["id"], max_depth)
    if not hops:
        console.print(
            f"[yellow]No call path[/yellow] from {a['qname']} to {b['qname']} "
            f"within {max_depth} hops."
        )
        console.print("[dim]They may be connected only through dynamic dispatch, "
                      "which static parsing cannot follow.[/dim]")
        raise typer.Exit(1)

    console.print(f"[bold]{len(hops) - 1}[/bold] hop(s) from {a['qname']} to {b['qname']}\n")
    for i, (row, line) in enumerate(hops):
        arrow = "   " if i == 0 else " -> "
        at = f"  [dim]{row['path']}:{line or row['start_line']}[/dim]"
        console.print(f"{arrow}{row['qname']}{at}")


def main():
    app()


if __name__ == "__main__":
    main()
