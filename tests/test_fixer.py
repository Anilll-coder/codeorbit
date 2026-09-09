"""Tests for fix generation, splicing and the verification gates.

The model is never called here. What matters is that everything AROUND the model
is safe: that a broken candidate is rejected, that a candidate which deletes the
symbol is rejected, and that nothing unverified can reach disk. Those properties
have to hold no matter what the model returns, so they are tested against
hand-written "model output" rather than a real generation.
"""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from codeorbit import fixer, verify
from codeorbit.audit import Finding
from codeorbit.rules import RULES_BY_ID


def w(root: Path, rel: str, body: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")
    return p


# ------------------------------------------------------------ reply parsing

def test_extract_block_from_fence():
    reply = "Here you go:\n```python\ndef f():\n    return 2\n```\nHope that helps."
    assert fixer.extract_block(reply) == "def f():\n    return 2"


def test_extract_block_without_fence():
    assert fixer.extract_block("def f():\n    return 2") == "def f():\n    return 2"


def test_extract_block_detects_no_change_needed():
    assert fixer.extract_block("NO CHANGE NEEDED") is None
    assert fixer.extract_block("This is fine. No change needed.") is None


# ------------------------------------------------------------------ splicing

def test_splice_replaces_only_the_symbol():
    src = "a = 1\n\ndef f():\n    return 1\n\nb = 2\n"
    out = fixer.splice(src, 3, 4, "def f():\n    return 99")
    assert "a = 1" in out and "b = 2" in out
    assert "return 99" in out and "return 1" not in out


def test_splice_reindents_a_flush_left_method():
    """Models return methods flush-left; spliced as-is that is a syntax error."""
    src = "class C:\n    def m(self):\n        return 1\n"
    out = fixer.splice(src, 2, 3, "def m(self):\n    return 2")
    assert "    def m(self):" in out
    assert "        return 2" in out
    import ast
    ast.parse(out)


def test_splice_rejects_a_span_outside_the_file():
    with pytest.raises(ValueError):
        fixer.splice("x = 1\n", 5, 9, "x = 2")


def test_splice_rejects_imports_only_reply():
    with pytest.raises(ValueError):
        fixer.splice("def f():\n    pass\n", 1, 2, "import os")


def test_leading_imports_are_hoisted_not_inlined():
    src = "import os\n\ndef f():\n    return os.getcwd()\n"
    out = fixer.splice(src, 3, 4, "import subprocess\n\ndef f():\n    return subprocess.run([])")
    lines = [l for l in out.splitlines() if l.strip()]
    assert lines[0].startswith("import")
    assert lines[1].startswith("import")
    assert "def f()" in out
    import ast
    ast.parse(out)


def test_existing_import_is_not_duplicated():
    src = "import subprocess\n\ndef f():\n    return 1\n"
    out = fixer.splice(src, 3, 4, "import subprocess\n\ndef f():\n    return subprocess.run([])")
    assert out.count("import subprocess") == 1


# ------------------------------------------------------------------- gates

def test_syntax_gate_rejects_broken_python():
    assert not verify.check_syntax("def f(:\n  pass\n", "python").ok


def test_syntax_gate_accepts_valid_python():
    assert verify.check_syntax("def f():\n    return 1\n", "python").ok


def test_symbol_gate_rejects_a_deleted_definition():
    """The worst failure mode: a 'fix' that removes the function."""
    g = verify.check_symbol_survives("x = 1\n", "python", "m.py", "m.f")
    assert not g.ok
    assert "gone" in g.detail


def test_symbol_gate_accepts_a_kept_definition():
    assert verify.check_symbol_survives(
        "def f():\n    return 2\n", "python", "m.py", "m.f"
    ).ok


def test_sandbox_verify_fails_fast_on_bad_syntax(tmp_path: Path):
    w(tmp_path, "m.py", "def f():\n    return 1\n")
    v = verify.sandbox_verify(tmp_path, "m.py", "def f(:\n", "python", "m.f",
                              with_tests=True)
    assert not v.ok
    # The test gate must never run once syntax has already failed.
    assert [g.name for g in v.gates] == ["syntax", "symbol"] or \
           v.gates[0].name == "syntax" and not v.gates[0].ok


# ------------------------------------------------------------------- applying

def _candidate(tmp_path: Path, ok: bool) -> fixer.FixCandidate:
    rule = RULES_BY_ID["py-eval"]
    finding = Finding(rule=rule, path="m.py", line=2, snippet="eval(x)")
    cand = fixer.FixCandidate(
        finding=finding, rel_path="m.py", lang="python", qname="m.f",
        start_line=1, end_line=2,
        original="def f(x):\n    return eval(x)",
        replacement="def f(x):\n    return int(x)",
        new_file_text="def f(x):\n    return int(x)\n",
    )
    cand.verdict = verify.Verdict([verify.GateResult("syntax", ok)])
    return cand


def test_apply_refuses_an_unverified_fix(tmp_path: Path):
    w(tmp_path, "m.py", "def f(x):\n    return eval(x)\n")
    cand = _candidate(tmp_path, ok=False)
    with pytest.raises(RuntimeError, match="verification"):
        fixer.apply(tmp_path, cand)
    assert "eval" in (tmp_path / "m.py").read_text(encoding="utf-8"), "file must be untouched"


def test_apply_writes_and_backs_up(tmp_path: Path):
    target = w(tmp_path, "m.py", "def f(x):\n    return eval(x)\n")
    cand = _candidate(tmp_path, ok=True)
    fixer.apply(tmp_path, cand)
    assert "int(x)" in target.read_text(encoding="utf-8")
    backup = tmp_path / "m.py.orig"
    assert backup.exists()
    assert "eval" in backup.read_text(encoding="utf-8"), "backup keeps the original"


# --------------------------------------------------------------- test runner

def test_detect_test_command_finds_pytest_layout(tmp_path: Path):
    (tmp_path / "tests").mkdir()
    cmd = verify.detect_test_command(tmp_path)
    assert cmd and "pytest" in " ".join(cmd)


def test_detect_test_command_returns_none_when_there_is_no_suite(tmp_path: Path):
    assert verify.detect_test_command(tmp_path) is None


def test_baseline_reports_a_red_suite(tmp_path: Path):
    (tmp_path / "tests").mkdir()
    w(tmp_path, "tests/test_x.py", "def test_fails():\n    assert False\n")
    assert not verify.baseline_tests_pass(tmp_path).ok


def test_baseline_reports_a_green_suite(tmp_path: Path):
    (tmp_path / "tests").mkdir()
    w(tmp_path, "tests/test_x.py", "def test_ok():\n    assert True\n")
    assert verify.baseline_tests_pass(tmp_path).ok
