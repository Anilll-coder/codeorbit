"""Console-script entry point, with a fast path for `codeorbit mcp`.

An MCP server is launched by an agent on every session start, and some clients
give the handshake a short window before declaring the server dead. So the
serving path must not pay for anything it does not use.

Going through cli.py costs a lot before a single line of `mcp` runs: typer and
rich are imported to build the command tree, and `from .indexer import ...`
pulls in the extractors, which construct four tree-sitter Language and Parser
objects at import time. The MCP server parses nothing - it reads a SQLite graph
that was built earlier - so all of that is dead weight on the one path where
latency is measurable.

This dispatches on argv before importing anything heavy. Every other command
still goes through the full CLI, where a few hundred milliseconds is invisible
next to the work it is about to do.
"""
from __future__ import annotations

import sys


def _fast_mcp() -> bool:
    """Serve MCP without importing the CLI. True if handled."""
    argv = sys.argv[1:]
    if not argv or argv[0] != "mcp":
        return False

    # Only the flags `mcp` actually takes. Anything else - a typo, --help -
    # falls through to the real CLI so the user still gets proper errors and
    # help text rather than a silent misparse.
    path = None
    i = 1
    while i < len(argv):
        a = argv[i]
        if a in ("-p", "--path") and i + 1 < len(argv):
            path = argv[i + 1]
            i += 2
            continue
        if a.startswith("--path="):
            path = a.split("=", 1)[1]
            i += 1
            continue
        return False        # unrecognised: let the CLI handle it
    from pathlib import Path

    from . import mcp_server

    root = Path(path).resolve() if path else Path.cwd()
    if not (root / ".codeorbit" / "graph.db").exists():
        print(f"[codeorbit-mcp] warning: {root} is not indexed yet. Tools will "
              f"return guidance until `codeorbit index` is run.",
              file=sys.stderr, flush=True)
    try:
        mcp_server.serve(root)
    except KeyboardInterrupt:
        pass
    return True


def main() -> None:
    if _fast_mcp():
        return
    from .cli import app
    app()
