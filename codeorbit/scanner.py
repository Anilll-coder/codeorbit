"""Walk a repository and decide which files to parse."""
from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

from . import langs

# Directories that are never source code worth graphing.
SKIP_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv",
    "env", "dist", "build", ".next", ".nuxt", "target", ".mypy_cache",
    ".pytest_cache", ".ruff_cache", "site-packages", ".codeorbit", ".tox",
    "coverage", ".idea", ".vscode", "vendor", "bower_components",
    # Added with the language expansion: these are build output for the
    # ecosystems that only became visible once their languages were readable,
    # and indexing generated code is worse than useless - it outweighs the
    # handwritten code and drowns the graph in symbols nobody wrote.
    ".gradle", ".m2", "Pods", "DerivedData", ".dart_tool", ".pub-cache",
    "_build", "deps", ".stack-work", ".cabal-sandbox",
    ".terraform", ".serverless", "cmake-build-debug", "cmake-build-release",
    ".cargo", "Godeps", ".bundle", "elm-stuff",
    "__snapshots__", ".svelte-kit", ".turbo", ".parcel-cache", ".angular",
}

# Generated or minified files, by name. These parse fine and say nothing.
SKIP_SUFFIXES = (
    ".min.js", ".min.css", ".bundle.js", ".d.ts", ".map",
    "_pb2.py", "_pb2_grpc.py", ".pb.go", "_generated.go", ".g.dart",
    ".freezed.dart", ".pb.cc", ".pb.h", "_pb.js", ".designer.cs",
    ".generated.cs", ".g.cs", "_test.pb.go",
)

MAX_BYTES = 1_500_000  # skip generated/minified monsters


def _git_tracked(root: Path) -> set[str] | None:
    """Prefer git's own idea of what is source. Returns None when git can't say.

    Returning None (rather than an empty set) matters for a directory that sits
    inside a repo but is ignored by it - a library under .venv/, a vendored
    checkout. There `git ls-files` succeeds and lists nothing, and treating that
    as "no file is tracked" would filter the entire target away and index
    nothing. An empty result means git has no opinion here, so fall back to
    walking the tree.

    `-z` is not optional. Without it git applies `core.quotePath` and renders
    any path outside ASCII as an escaped, double-quoted C string - so on a repo
    with an accented or CJK filename the path never matches the one the walk
    produced, and those files were silently dropped from every index. NUL
    separation turns that off and needs no unquoting.
    """
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z"],
            capture_output=True, timeout=30,
        )
        if out.returncode != 0:
            return None
        tracked = {
            raw.decode("utf-8", errors="surrogateescape")
            for raw in out.stdout.split(b"\0") if raw
        }
        return tracked or None
    except Exception:
        return None


def sha1(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def lang_of(path: Path) -> str | None:
    """The language CodeOrbit would parse this file as, or None.

    Separate from `scan` so the CLI can explain a single file: "that is a
    Kotlin file and no Kotlin grammar is installed" is a better answer than an
    index that quietly skipped it.
    """
    name = path.name
    if name.endswith(SKIP_SUFFIXES):
        return None
    return langs.lang_for(name)


def scan(root: Path, skipped: dict[str, int] | None = None
         ) -> list[tuple[Path, str]]:
    """Return [(absolute_path, lang)] for every source file worth indexing.

    Pass `skipped` to also collect a count per language that this machine has
    no grammar for. It is filled during the same walk rather than by a second
    one, because on a large repository walking the tree twice to produce one
    line of output is not worth it.
    """
    root = root.resolve()
    tracked = _git_tracked(root)
    supported = langs.supported()
    found: list[tuple[Path, str]] = []

    for p in root.rglob("*"):
        if not p.is_file():
            continue
        lang = lang_of(p)
        if lang is None:
            continue
        rel_parts = p.relative_to(root).parts
        if any(part in SKIP_DIRS for part in rel_parts[:-1]):
            continue
        # A language with no grammar on this machine is skipped rather than
        # indexed to an empty module: a file row with no symbols looks like a
        # file with nothing in it, which is a lie the graph then repeats.
        if lang not in supported:
            if skipped is not None:
                skipped[lang] = skipped.get(lang, 0) + 1
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


def unsupported(root: Path, limit: int = 12) -> list[tuple[str, int]]:
    """Languages present in the tree that no installed grammar can read.

    What `codeorbit index` reports when it skipped things, so a user whose
    Kotlin project indexed to nothing is told why rather than concluding the
    tool is broken.
    """
    counts: dict[str, int] = {}
    scan(root, skipped=counts)
    return sorted(counts.items(), key=lambda kv: -kv[1])[:limit]
