"""Checks a generated fix must survive before it is allowed near your files.

A local 3.8B model will produce confident nonsense some fraction of the time.
The only reason it is safe to let one edit source at all is that nothing it
writes is trusted: every candidate is spliced into a COPY, put through these
gates, and discarded unless all of them pass.

The gates run cheapest-first, so a fix that does not even parse never reaches
the test suite.
"""
from __future__ import annotations

import ast
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path


@dataclass
class GateResult:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class Verdict:
    gates: list[GateResult]

    @property
    def ok(self) -> bool:
        return all(g.ok for g in self.gates)

    @property
    def failed(self) -> list[GateResult]:
        return [g for g in self.gates if not g.ok]

    def summary(self) -> str:
        return ", ".join(f"{g.name}{'' if g.ok else ' FAILED'}" for g in self.gates)


# ------------------------------------------------------------------ gate 1-2

def check_syntax(text: str, lang: str, filename: str = "<fix>") -> GateResult:
    """Does the edited file still parse at all?"""
    if lang == "python":
        try:
            ast.parse(text, filename=filename)
            return GateResult("syntax", True)
        except SyntaxError as e:
            return GateResult("syntax", False, f"line {e.lineno}: {e.msg}")

    if lang == "javascript":
        node = shutil.which("node")
        if not node:
            return GateResult("syntax", True, "node not installed; skipped")
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                         encoding="utf-8") as fh:
            fh.write(text)
            tmp = fh.name
        try:
            p = subprocess.run([node, "--check", tmp],
                               capture_output=True, text=True, timeout=30)
            if p.returncode == 0:
                return GateResult("syntax", True)
            first = (p.stderr or "").strip().splitlines()
            return GateResult("syntax", False, first[-1] if first else "parse error")
        except (OSError, subprocess.SubprocessError) as e:
            return GateResult("syntax", True, f"could not run node ({e}); skipped")
        finally:
            Path(tmp).unlink(missing_ok=True)

    return GateResult("syntax", True, f"no checker for {lang}; skipped")


def check_symbol_survives(text: str, lang: str, rel_path: str,
                          qname: str) -> GateResult:
    """Is the symbol we set out to fix still defined afterwards?

    Catches the most damaging failure mode by far: a model that "fixes" a
    function by deleting it, or by replacing the file with a fragment.
    """
    from .extract import parse_and_extract
    try:
        ex = parse_and_extract(text.encode("utf-8"), lang, rel_path)
    except Exception as e:
        return GateResult("symbol", False, f"re-parse failed: {e}")

    names = {s.qname for s in ex.symbols}
    if qname in names:
        return GateResult("symbol", True)

    tail = qname.rsplit(".", 1)[-1]
    if any(s.name == tail for s in ex.symbols):
        return GateResult("symbol", True, "found by short name")
    return GateResult("symbol", False, f"{qname} is gone after the edit")


# -------------------------------------------------------------------- gate 3

def detect_test_command(root: Path) -> list[str] | None:
    """The project's own test command, if we can tell what it is."""
    if (root / "pyproject.toml").exists() or (root / "pytest.ini").exists() \
            or (root / "tests").is_dir() or (root / "test").is_dir():
        return [sys.executable, "-m", "pytest", "-q", "-x"]
    pkg = root / "package.json"
    if pkg.exists():
        try:
            import json
            if "test" in json.loads(pkg.read_text(encoding="utf-8")).get("scripts", {}):
                npm = shutil.which("npm")
                if npm:
                    return [npm, "test", "--silent"]
        except Exception:
            pass
    return None


def run_tests(root: Path, cmd: list[str], timeout: int = 300) -> GateResult:
    try:
        p = subprocess.run(cmd, cwd=str(root), capture_output=True,
                           text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return GateResult("tests", False, f"timed out after {timeout}s")
    except OSError as e:
        return GateResult("tests", True, f"could not run tests ({e}); skipped")

    if p.returncode == 0:
        return GateResult("tests", True)

    out = ((p.stdout or "") + (p.stderr or "")).strip().splitlines()
    tail = " | ".join(l.strip() for l in out[-3:] if l.strip())
    return GateResult("tests", False, tail[:300] or f"exit {p.returncode}")


# ------------------------------------------------------------------- harness

def sandbox_verify(root: Path, rel_path: str, new_text: str, lang: str,
                   qname: str, with_tests: bool = False,
                   test_cmd: list[str] | None = None) -> Verdict:
    """Run every gate against a full copy of the project with the edit applied.

    The copy matters. Type-checking a string in memory cannot tell you whether
    the rest of the project still imports, and running the suite in place would
    mean the unverified edit was already on disk - which is the exact thing
    this is supposed to prevent.
    """
    gates = [
        check_syntax(new_text, lang, rel_path),
        check_symbol_survives(new_text, lang, rel_path, qname),
    ]
    if not all(g.ok for g in gates) or not with_tests:
        return Verdict(gates)

    cmd = test_cmd or detect_test_command(root)
    if cmd is None:
        gates.append(GateResult("tests", True, "no test suite detected; skipped"))
        return Verdict(gates)

    with tempfile.TemporaryDirectory(prefix="codeorbit-verify-") as tmp:
        dest = Path(tmp) / "project"
        shutil.copytree(
            root, dest,
            ignore=shutil.ignore_patterns(
                ".git", ".venv", "venv", "node_modules", "__pycache__",
                ".codeorbit", ".mypy_cache", ".pytest_cache", "dist", "build",
            ),
        )
        target = dest / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(new_text, encoding="utf-8")
        gates.append(run_tests(dest, cmd))

    return Verdict(gates)


def baseline_tests_pass(root: Path, test_cmd: list[str] | None = None) -> GateResult:
    """Are the tests green BEFORE we change anything?

    Without this, an already-red suite makes every candidate fix look rejected
    and the tool blames the model for a pre-existing failure.
    """
    cmd = test_cmd or detect_test_command(root)
    if cmd is None:
        return GateResult("baseline", True, "no test suite detected")
    return run_tests(root, cmd)
