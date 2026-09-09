"""Find bugs, security issues and quality problems - ranked by what they reach.

A linter gives you a flat list. That list is useless on a real codebase because
it does not say which of 400 findings to fix first.

The graph answers that. Every finding is attributed to the symbol that contains
it, and the symbol carries two facts a linter does not have: how much code
transitively depends on it, and whether any test reaches it. An `eval` inside a
function that 200 things call and no test covers is a different problem from the
same `eval` in dead code, and this ranks them accordingly.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from . import query
from .rules import RULES_BY_ID, SEVERITY_ORDER, Rule, is_test_path, rules_for

# Findings inside a string that is obviously an example or a rule definition
# would make this file report itself. Cheap guard, worth it.
SELF_MARKERS = ("codeorbit/rules.py", "codeorbit\\rules.py")


@dataclass
class Finding:
    rule: Rule
    path: str
    line: int
    snippet: str
    symbol: str | None = None
    symbol_id: int | None = None
    symbol_kind: str | None = None
    blast: int = 0
    tested: bool = False
    in_test_file: bool = False

    @property
    def risk(self) -> int:
        """Ordering key: severity first, then reach, then coverage."""
        sev = SEVERITY_ORDER[self.rule.severity] * 1_000_000
        reach = -min(self.blast, 999) * 1000
        untested = 0 if self.tested else -500
        return sev + reach + untested


@dataclass
class AuditReport:
    findings: list[Finding] = field(default_factory=list)
    files_scanned: int = 0
    lines_scanned: int = 0

    def by_severity(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for f in self.findings:
            out[f.rule.severity] = out.get(f.rule.severity, 0) + 1
        return out


def _symbol_at(index: list, line: int):
    """Innermost symbol whose span contains `line`.

    `index` is this file's non-module nodes sorted by span width descending, so
    the last match is the tightest - a method wins over the class holding it.
    """
    hit = None
    for row in index:
        if row["start_line"] <= line <= row["end_line"]:
            hit = row
    return hit


def scan_file(path: Path, rel: str, lang: str) -> list[Finding]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []

    lines = text.splitlines()
    in_test = is_test_path(rel)
    out: list[Finding] = []

    applicable = [r for r in rules_for(lang) if not (r.skip_tests and in_test)]

    # Multi-line rules need the whole text; single-line ones are far cheaper
    # per line. Split them so the common case stays fast.
    multiline = [r for r in applicable if "\\n" in r.pattern.pattern]
    single = [r for r in applicable if r not in multiline]

    for i, line in enumerate(lines, 1):
        if len(line) > 500:          # minified or generated; regexes go quadratic
            continue
        for rule in single:
            if rule.pattern.search(line):
                out.append(Finding(
                    rule=rule, path=rel, line=i,
                    snippet=line.strip()[:160], in_test_file=in_test,
                ))

    for rule in multiline:
        for m in rule.pattern.finditer(text):
            line_no = text.count("\n", 0, m.start()) + 1
            out.append(Finding(
                rule=rule, path=rel, line=line_no,
                snippet=lines[line_no - 1].strip()[:160] if line_no <= len(lines) else "",
                in_test_file=in_test,
            ))

    return out


def audit_project(root: Path, conn, include_tests: bool = False,
                  min_severity: str = "low") -> AuditReport:
    root = root.resolve()
    report = AuditReport()

    files = conn.execute("SELECT id, path, lang, loc FROM files ORDER BY path").fetchall()
    threshold = SEVERITY_ORDER[min_severity]

    for f in files:
        rel = f["path"]
        if any(m in rel for m in SELF_MARKERS):
            continue
        if not include_tests and is_test_path(rel):
            continue

        report.files_scanned += 1
        report.lines_scanned += f["loc"] or 0

        found = scan_file(root / rel, rel, f["lang"])
        found = [x for x in found if SEVERITY_ORDER[x.rule.severity] <= threshold]
        if not found:
            continue

        index = conn.execute(
            "SELECT id, qname, kind, start_line, end_line FROM nodes "
            "WHERE file_id = ? AND kind != 'module' "
            "ORDER BY (end_line - start_line) DESC",
            (f["id"],),
        ).fetchall()

        for finding in found:
            sym = _symbol_at(index, finding.line)
            if sym is not None:
                finding.symbol = sym["qname"]
                finding.symbol_id = sym["id"]
                finding.symbol_kind = sym["kind"]
                finding.blast = len(query.impact(conn, sym["id"], max_depth=3))
                finding.tested = bool(query.affected_tests(conn, sym["id"], max_depth=4))
            report.findings.append(finding)

    report.findings.sort(key=lambda x: (x.risk, x.path, x.line))
    return report


# ---------------------------------------------------------------- explanation

SYSTEM = (
    "You are a security and code-quality reviewer. You are given findings from a "
    "static scan of the user's repository, each with the source line, the symbol "
    "that contains it, how much code transitively depends on that symbol (blast "
    "radius), and whether any test reaches it.\n"
    "Rules:\n"
    "1. Judge ONLY the findings and code shown. Never invent a file, caller or "
    "vulnerability that is not listed.\n"
    "2. Say plainly which findings are real problems and which are likely false "
    "positives in this context - a scan cannot tell the difference, you should.\n"
    "3. Weigh by reach: a flaw in something widely depended on and untested "
    "matters more than the same flaw in isolated code.\n"
    "4. For each real problem give the concrete fix, in code where it is short.\n"
    "5. Be specific and brief. No preamble, no restating the finding list."
)


def build_prompt(root: Path, conn, findings: list[Finding],
                 max_findings: int = 6, body_lines: int = 25) -> str:
    parts = ["## Static scan findings, most severe and most-reached first", ""]

    for i, f in enumerate(findings[:max_findings], 1):
        parts.append(f"### {i}. [{f.rule.severity.upper()}] {f.rule.title}")
        parts.append(f"rule: {f.rule.id}")
        parts.append(f"at: {f.path}:{f.line}")
        parts.append(f"code: {f.snippet}")
        parts.append(f"concern: {f.rule.why}")
        if f.symbol:
            parts.append(
                f"inside: {f.symbol_kind} {f.symbol} "
                f"(blast radius {f.blast}, {'tested' if f.tested else 'NO test reaches it'})"
            )
            row = conn.execute(
                "SELECT n.id, n.name, n.qname, n.kind, n.start_line, n.end_line, "
                "n.signature, n.docstring, fl.path AS path, fl.lang AS lang "
                "FROM nodes n JOIN files fl ON fl.id = n.file_id WHERE n.id = ?",
                (f.symbol_id,),
            ).fetchone()
            if row is not None:
                body = query.source_of(root, row, max_lines=body_lines)
                if body:
                    parts.append(f"context:\n```{row['lang']}\n{body}\n```")
        else:
            parts.append("inside: module level (no enclosing function)")
        parts.append("")

    parts += ["----", "Which of these are real, and how should each be fixed?"]
    return "\n".join(parts)
