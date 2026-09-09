"""Generate a fix for a finding, verify it, and only then offer to apply it.

The order is the whole design. A local 3.8B model is not trustworthy enough to
edit source directly, so nothing it produces is applied on the strength of the
model's confidence: a candidate is spliced into a copy, run through
`verify.sandbox_verify`, and thrown away unless every gate passes. Applying is
opt-in on top of that, and always leaves a .orig backup.

The model is also given the smallest possible job - rewrite ONE symbol, return
only that symbol - because asking a small model for a whole-file rewrite is how
you lose unrelated code.
"""
from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from . import llm, verify
from .audit import Finding

FENCE = re.compile(r"```[a-zA-Z0-9_+-]*\s*\n(.*?)```", re.S)


@dataclass
class FixCandidate:
    finding: Finding
    rel_path: str
    lang: str
    qname: str
    start_line: int
    end_line: int
    original: str
    replacement: str | None = None
    new_file_text: str | None = None
    verdict: verify.Verdict | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return bool(self.replacement) and self.verdict is not None and self.verdict.ok


SYSTEM = (
    "You rewrite a single function or method to fix one specific problem.\n"
    "Hard rules:\n"
    "1. Output ONLY the rewritten definition, inside one fenced code block. No "
    "explanation before or after.\n"
    "2. Keep the same name, the same parameters and the same indentation level "
    "as the original. It is spliced back in exactly where the original was.\n"
    "3. Change only what is needed for the stated problem. Do not reformat, "
    "rename, or 'improve' anything else.\n"
    "4. Do not add imports at the top of the file - you cannot see the file. If "
    "the fix needs a new import, use a fully qualified call or a local import "
    "inside the function.\n"
    "5. If the finding is a false positive and the code is correct as written, "
    "reply with exactly: NO CHANGE NEEDED"
)


def extract_block(text: str) -> str | None:
    """Pull the code out of the model's reply."""
    if "NO CHANGE NEEDED" in text.upper():
        return None
    m = FENCE.search(text)
    body = m.group(1) if m else text
    body = body.strip("\n")
    return body if body.strip() else None


def _dedent_to(block: str, indent: str) -> str:
    """Re-indent a returned definition to sit where the original sat.

    Models routinely return a method flush-left. Splicing that into a class body
    is a syntax error, and it is the single most common way a candidate dies -
    so normalise instead of rejecting.
    """
    lines = block.splitlines()
    if not lines:
        return block
    first = lines[0]
    existing = first[: len(first) - len(first.lstrip())]
    if existing == indent:
        return block

    out = []
    for line in lines:
        if not line.strip():
            out.append("")
            continue
        stripped = line[len(existing):] if line.startswith(existing) else line.lstrip()
        out.append(indent + stripped)
    return "\n".join(out)


IMPORT_LINE = re.compile(r"^\s*(?:import\s+\S|from\s+\S+\s+import\s)")


def split_leading_imports(block: str) -> tuple[list[str], str]:
    """Peel import lines off the front of a returned block.

    Models add the import a fix needs even when told not to. Left in place it
    would be spliced into the middle of the file, at the symbol's position -
    syntactically legal at module level, which is why the syntax gate does not
    catch it, and a duplicate import besides. So they are lifted out here and
    re-inserted where imports actually belong.
    """
    lines = block.splitlines()
    imports: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        if IMPORT_LINE.match(line):
            imports.append(line.strip())
            i += 1
            continue
        break
    return imports, "\n".join(lines[i:])


def merge_imports(file_text: str, imports: list[str]) -> str:
    """Add imports the file does not already have, after its existing ones."""
    if not imports:
        return file_text
    lines = file_text.splitlines()
    existing = {l.strip() for l in lines if IMPORT_LINE.match(l)}
    missing = [imp for imp in imports if imp not in existing]
    if not missing:
        return file_text

    last = -1
    for idx, line in enumerate(lines):
        if IMPORT_LINE.match(line):
            last = idx
        elif last >= 0 and line.strip() and not line.startswith((" ", "\t")):
            break                      # first real statement after the imports

    at = last + 1 if last >= 0 else 0
    return "\n".join(lines[:at] + missing + lines[at:])


def splice(file_text: str, start_line: int, end_line: int, block: str) -> str:
    """Replace lines [start_line, end_line] with `block`, keeping indentation."""
    lines = file_text.splitlines()
    if start_line < 1 or end_line > len(lines) or start_line > end_line:
        raise ValueError("symbol span is outside the file")

    imports, body = split_leading_imports(block)
    if not body.strip():
        raise ValueError("the model returned only imports, no definition")

    original_first = lines[start_line - 1]
    indent = original_first[: len(original_first) - len(original_first.lstrip())]
    body = _dedent_to(body, indent)

    merged = "\n".join(lines[: start_line - 1] + body.splitlines() + lines[end_line:])
    return merge_imports(merged, imports) + "\n"


def build_prompt(finding: Finding, source: str, lang: str) -> str:
    return (
        f"Problem: {finding.rule.title}\n"
        f"Why it matters: {finding.rule.why}\n"
        f"Offending line ({finding.path}:{finding.line}): {finding.snippet}\n\n"
        f"Rewrite this {lang} definition to fix it:\n\n"
        f"```{lang}\n{source}\n```"
    )


def propose(root: Path, conn, finding: Finding, model: str = llm.DEFAULT_MODEL,
            max_tokens: int = 500) -> FixCandidate | None:
    """Ask the model for a fix and verify it. Never writes to disk."""
    if finding.symbol_id is None:
        return None

    row = conn.execute(
        "SELECT n.qname, n.start_line, n.end_line, f.path AS path, f.lang AS lang "
        "FROM nodes n JOIN files f ON f.id = n.file_id WHERE n.id = ?",
        (finding.symbol_id,),
    ).fetchone()
    if row is None:
        return None

    target = root / row["path"]
    try:
        file_text = target.read_text(encoding="utf-8")
    except OSError as e:
        return None

    lines = file_text.splitlines()
    if row["end_line"] > len(lines):
        return None
    original = "\n".join(lines[row["start_line"] - 1: row["end_line"]])

    cand = FixCandidate(
        finding=finding, rel_path=row["path"], lang=row["lang"],
        qname=row["qname"], start_line=row["start_line"],
        end_line=row["end_line"], original=original,
    )

    try:
        reply = llm.generate(
            build_prompt(finding, original, row["lang"]),
            model=model, system=SYSTEM, num_predict=max_tokens,
        )
    except llm.OllamaError as e:
        cand.error = str(e)
        return cand

    block = extract_block(reply)
    if block is None:
        cand.error = "model reported no change needed"
        return cand
    if block.strip() == original.strip():
        cand.error = "model returned the code unchanged"
        return cand

    cand.replacement = block
    try:
        cand.new_file_text = splice(file_text, row["start_line"], row["end_line"], block)
    except ValueError as e:
        cand.error = str(e)
        return cand

    return cand


def verify_candidate(root: Path, cand: FixCandidate, with_tests: bool,
                     test_cmd: list[str] | None = None) -> FixCandidate:
    if cand.new_file_text is None:
        return cand
    cand.verdict = verify.sandbox_verify(
        root, cand.rel_path, cand.new_file_text, cand.lang, cand.qname,
        with_tests=with_tests, test_cmd=test_cmd,
    )
    return cand


def apply(root: Path, cand: FixCandidate, backup: bool = True) -> Path:
    """Write a verified fix. Refuses anything that did not pass its gates."""
    if not cand.ok:
        raise RuntimeError("refusing to apply a fix that did not pass verification")
    target = root / cand.rel_path
    if backup:
        shutil.copy2(target, target.with_suffix(target.suffix + ".orig"))
    target.write_text(cand.new_file_text, encoding="utf-8")
    return target


def diff_lines(cand: FixCandidate) -> list[str]:
    """Unified diff of just the replaced symbol, for display."""
    import difflib
    return list(difflib.unified_diff(
        cand.original.splitlines(),
        (cand.replacement or "").splitlines(),
        fromfile=f"{cand.rel_path}:{cand.start_line} (before)",
        tofile=f"{cand.rel_path}:{cand.start_line} (after)",
        lineterm="", n=2,
    ))
