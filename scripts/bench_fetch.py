"""Check out each benchmark dataset's repository at its pinned commit, then index it.

A dataset whose pin is {"commit": ...} is fetched into bench/repos/<name> (not
committed - the pin is what makes it reproducible). Datasets pinned to a PyPI
version, like the original rich set, are skipped: install that version instead.

    python scripts/bench_fetch.py                  # every dataset
    python scripts/bench_fetch.py bench/datasets/click.json --embed

--embed also builds embeddings (needs Ollama running), without which the dense,
hybrid and full CodeOrbit arms cannot run.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


def git(*args: str, cwd: Path | None = None) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout.strip()


def checkout(url: str, commit: str, dest: Path) -> None:
    """Fetch exactly one commit. Cheap even for large histories."""
    if not (dest / ".git").exists():
        dest.mkdir(parents=True, exist_ok=True)
        git("init", "-q", cwd=dest)
        git("remote", "add", "origin", url, cwd=dest)
    current = subprocess.run(["git", "rev-parse", "-q", "--verify", "HEAD"], cwd=dest,
                             capture_output=True, text=True).stdout.strip()
    if current.startswith(commit):
        return
    git("fetch", "-q", "--depth", "1", "origin", commit, cwd=dest)
    git("checkout", "-q", "--detach", "FETCH_HEAD", cwd=dest)
    head = git("rev-parse", "HEAD", cwd=dest)
    if not head.startswith(commit):
        sys.exit(f"{url}: fetched {head}, wanted {commit}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("datasets", nargs="*",
                    help="dataset files (default: bench/datasets/*.json)")
    ap.add_argument("--embed", action="store_true", help="also build embeddings")
    args = ap.parse_args()

    from codeorbit.indexer import index_project
    from codeorbit.resolve import resolve_project

    paths = [Path(p) for p in args.datasets] or sorted((REPO / "bench/datasets").glob("*.json"))
    for path in paths:
        data = json.loads(path.read_text(encoding="utf-8"))
        repo, pin = data["repo"], data["repo"].get("pin", {})
        if "commit" not in pin:
            print(f"- {data['name']}: pinned by {pin or 'nothing'}, not a git checkout; skipped")
            continue

        dest = (REPO / repo["default_path"]).resolve()
        print(f"- {data['name']}: {repo['url']} @ {pin['commit'][:12]} -> {dest}")
        checkout(repo["url"], pin["commit"], dest)

        index_project(dest, full=True)
        st = resolve_project(dest)
        print(f"    {st['files']} files, {st['nodes']} symbols, {st['edges']} edges")

        if args.embed:
            from codeorbit import db, semantic
            conn = db.connect(dest)
            out = semantic.build(conn, dest)
            conn.close()
            print(f"    embedded {out['embedded']} ({out['failed']} failed)")


if __name__ == "__main__":
    main()
