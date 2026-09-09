"""Review a change with the graph around it, not just the diff.

A plain diff tells a reviewer what changed. It cannot tell them what the change
*reaches* - and that is where the real risk lives: the eleven callers that pass
two arguments to a function that now takes three, the test that never runs the
branch you edited.

So this walks: diff -> changed line ranges -> the symbols those lines belong to
-> each symbol's callers, blast radius and covering tests. The model is then
asked to review the change *against that neighbourhood*. Everything it is told
about the surroundings comes from the graph, so it is grounded rather than
guessed.
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from . import query

HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


class GitError(RuntimeError):
    pass


@dataclass
class ChangedSymbol:
    row: object                     # sqlite Row for the node
    added: int = 0                  # lines added inside this symbol
    removed: int = 0
    callers: list = field(default_factory=list)
    blast: int = 0
    tests: list = field(default_factory=list)


def _git(root: Path, *args: str) -> str:
    try:
        p = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True, text=True, timeout=60,
        )
    except FileNotFoundError as e:
        raise GitError("git is not installed or not on PATH.") from e
    if p.returncode != 0:
        raise GitError((p.stderr or p.stdout).strip() or "git failed")
    return p.stdout


def is_repo(root: Path) -> bool:
    try:
        _git(root, "rev-parse", "--git-dir")
        return True
    except GitError:
        return False


def diff_text(root: Path, base: str | None, staged: bool) -> str:
    args = ["diff", "--no-color", "--no-ext-diff"]
    if staged:
        args.append("--cached")
    if base:
        args.append(base)
    return _git(root, *args)


def changed_ranges(diff: str) -> dict[str, list[tuple[int, int]]]:
    """Per-file line ranges touched on the NEW side of the diff.

    --unified=0 is not required: hunk headers already carry the new-side start
    and length, which is all we need to intersect against symbol spans.
    """
    out: dict[str, list[tuple[int, int]]] = {}
    current: str | None = None
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            current = line[6:].strip()
            out.setdefault(current, [])
            continue
        if line.startswith("+++ /dev/null"):
            current = None          # file deleted; nothing on the new side
            continue
        if current and line.startswith("@@"):
            m = HUNK.match(line)
            if m:
                start = int(m.group(1))
                length = int(m.group(2) or 1)
                if length > 0:
                    out[current].append((start, start + length - 1))
    return {k: v for k, v in out.items() if v}


def diff_stats(diff: str) -> dict[str, tuple[int, int]]:
    """Per-file (added, removed) counts."""
    stats: dict[str, tuple[int, int]] = {}
    current = None
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            current = line[6:].strip()
            stats.setdefault(current, (0, 0))
        elif current:
            if line.startswith("+") and not line.startswith("+++"):
                a, r = stats[current]
                stats[current] = (a + 1, r)
            elif line.startswith("-") and not line.startswith("---"):
                a, r = stats[current]
                stats[current] = (a, r + 1)
    return stats


def symbols_for(conn, path: str, ranges: list[tuple[int, int]]) -> list:
    """Definitions in `path` whose span overlaps any changed range.

    Modules are excluded unless nothing else matched: a module node spans the
    whole file, so it always overlaps and would drown the real symbols.
    """
    rows = conn.execute(
        "SELECT n.id, n.name, n.qname, n.kind, n.start_line, n.end_line, "
        "n.signature, n.docstring, f.path AS path, f.lang AS lang "
        "FROM nodes n JOIN files f ON f.id = n.file_id "
        "WHERE f.path = ? ORDER BY n.start_line",
        (path,),
    ).fetchall()

    def overlaps(r):
        return any(not (e < r["start_line"] or s > r["end_line"]) for s, e in ranges)

    hits = [r for r in rows if r["kind"] != "module" and overlaps(r)]
    if hits:
        # Drop a class when one of its own methods also matched - the method is
        # the specific thing that changed.
        method_parents = {h["qname"].rsplit(".", 1)[0] for h in hits if h["kind"] == "method"}
        hits = [h for h in hits if not (h["kind"] == "class" and h["qname"] in method_parents)]
        return hits
    return [r for r in rows if r["kind"] == "module" and overlaps(r)]


def collect(root: Path, conn, base: str | None, staged: bool,
            max_symbols: int = 6) -> tuple[str, list[ChangedSymbol], dict]:
    """Return (diff_text, changed symbols with graph context, summary)."""
    diff = diff_text(root, base, staged)
    if not diff.strip():
        return "", [], {"files": 0, "added": 0, "removed": 0, "unindexed": []}

    ranges = changed_ranges(diff)
    stats = diff_stats(diff)

    known = {r["path"] for r in conn.execute("SELECT path FROM files")}
    unindexed = [p for p in ranges if p not in known]

    found: list[ChangedSymbol] = []
    for path, rs in ranges.items():
        if path not in known:
            continue
        for row in symbols_for(conn, path, rs):
            cs = ChangedSymbol(row=row)
            cs.callers = query.callers(conn, row["id"], limit=8)
            cs.blast = len(query.impact(conn, row["id"], max_depth=3))
            cs.tests = query.affected_tests(conn, row["id"], max_depth=4)
            found.append(cs)

    # Riskiest first: what a change reaches matters more than how big it is.
    found.sort(key=lambda c: (-c.blast, -len(c.callers)))

    summary = {
        "files": len(ranges),
        "added": sum(a for a, _ in stats.values()),
        "removed": sum(r for _, r in stats.values()),
        "symbols": len(found),
        "unindexed": unindexed,
    }
    return diff, found[:max_symbols], summary


SYSTEM = (
    "You are reviewing a code change. You are given the diff, and - retrieved "
    "from a knowledge graph of the repository - the symbols it touches with "
    "their callers, blast radius and covering tests.\n"
    "Rules:\n"
    "1. Review ONLY what the diff and the graph context show. Never invent a "
    "caller, file or behaviour that is not listed.\n"
    "2. Prioritise by what the change REACHES. A function with many callers or "
    "no test coverage is higher risk than a big but isolated edit.\n"
    "3. Call out signature or contract changes that the listed callers would "
    "break.\n"
    "4. Say when a changed symbol has no covering test.\n"
    "5. Be specific: name symbols and file:line. No preamble, no restating the "
    "diff. If the change looks fine, say so briefly.\n"
    "Format: a short verdict line, then bullets ordered most to least serious."
)


def build_prompt(root: Path, diff: str, changed: list[ChangedSymbol],
                 summary: dict, body_lines: int = 40,
                 max_diff_chars: int = 6000) -> str:
    parts = [
        "## Change summary",
        f"{summary['files']} file(s), +{summary['added']} -{summary['removed']} lines",
        "",
        "## Diff",
        "```diff",
        diff[:max_diff_chars] + ("\n... (diff truncated)" if len(diff) > max_diff_chars else ""),
        "```",
        "",
        "## What the change touches, from the code graph",
    ]

    if not changed:
        parts.append("(no indexed symbols matched - the graph cannot say what this reaches)")

    for cs in changed:
        r = cs.row
        parts.append(
            f"\n### {r['kind']} {r['qname']}  ({r['path']}:{r['start_line']}-{r['end_line']})"
        )
        if r["signature"]:
            parts.append(f"signature: {r['signature']}")
        parts.append(f"blast radius: {cs.blast} symbol(s) transitively reach this")

        if cs.callers:
            parts.append("direct callers:")
            for c in cs.callers:
                mark = "" if c["resolution"] == "exact" else "  [uncertain]"
                parts.append(f"  - {c['qname']} ({c['path']}:{c['call_line']}){mark}")
        else:
            parts.append("direct callers: none in the index")

        if cs.tests:
            parts.append("covering tests: " + ", ".join(t["qname"] for t in cs.tests[:6]))
        else:
            parts.append("covering tests: NONE FOUND")

        body = query.source_of(root, r, max_lines=body_lines)
        if body:
            parts.append(f"current source:\n```{r['lang']}\n{body}\n```")

    if summary["unindexed"]:
        parts.append(
            "\nNote: these changed files are not in the index, so nothing is "
            "known about what they reach: " + ", ".join(summary["unindexed"][:8])
        )

    parts += ["", "----", "Review this change."]
    return "\n".join(parts)
