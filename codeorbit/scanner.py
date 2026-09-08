"""Walk a repository and decide which files to parse."""
from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

LANG_BY_SUFFIX = {
    ".py": "python",
    ".js": "javascript", ".jsx": "javascript",
    ".mjs": "javascript", ".cjs": "javascript",
}

# Directories that are never source code worth graphing.
SKIP_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv",
    "env", "dist", "build", ".next", ".nuxt", "target", ".mypy_cache",
    ".pytest_cache", ".ruff_cache", "site-packages", ".codeorbit", ".tox",
    "coverage", ".idea", ".vscode", "vendor", "bower_components",
}

MAX_BYTES = 1_500_000  # skip generated/minified monsters


def _git_tracked(root: Path) -> set[str] | None:
    """Prefer git's own idea of what is source. Returns None outside a repo."""
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "ls-files"],
            capture_output=True, text=True, timeout=30,
        )
        if out.returncode != 0:
            return None
        return {line.strip() for line in out.stdout.splitlines() if line.strip()}
    except Exception:
        return None


def sha1(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def scan(root: Path) -> list[tuple[Path, str]]:
    """Return [(absolute_path, lang)] for every source file worth indexing."""
    root = root.resolve()
    tracked = _git_tracked(root)
    found: list[tuple[Path, str]] = []

    for p in root.rglob("*"):
        if not p.is_file():
            continue
        lang = LANG_BY_SUFFIX.get(p.suffix.lower())
        if lang is None:
            continue
        rel_parts = p.relative_to(root).parts
        if any(part in SKIP_DIRS for part in rel_parts[:-1]):
            continue
        if p.name.endswith((".min.js", ".bundle.js", ".d.ts")):
            continue
        try:
            if p.stat().st_size > MAX_BYTES:
                continue
        except OSError:
            continue
        if tracked is not None:
            rel = "/".join(rel_parts)
            if rel not in tracked:
                continue  # untracked / gitignored
        found.append((p, lang))

    return sorted(found)
