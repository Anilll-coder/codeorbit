"""Context injection: CodeOrbit's ranked code, added to the prompt before the
model's first turn.

Measured with Claude Code (bench/agent_runs/*-claude-sonnet*): a strong model
finds the code in about three turns with grep, and every turn re-sends the
agent's own large system prompt. A tool, however lean, still costs a turn to
call, so as a tool CodeOrbit could at best match grep (the lean profile on 30
held-out issues: -3% mean input tokens, the 95% interval spanning zero).

What does not cost a turn is context that is already there. Editors with
codebase awareness (Cursor, Copilot) attach retrieved code to the request
automatically; Claude Code allows the same through a UserPromptSubmit hook,
whose output is added to the prompt. This module is that hook: it ranks the
functions the request is about and prints the lean `find` answer - locations,
signatures, the best match's relevant lines - in about 500 tokens. No tool
definitions, no tool call, no extra turn.

Hook input is JSON on stdin with the user's `prompt` and the session's `cwd`.
Everything else - a missing index, an empty prompt, any failure - prints
nothing, because a hook must never block or confuse the request it serves.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

PROMPT_CHARS = 4000
HEADER = ("CodeOrbit (automatic, from this repository's call graph): the code this "
          "request is most likely about, ranked, best first. Line numbers are "
          "current; the excerpt is the file's real content.\n")


def context_for(root: Path, prompt: str) -> str:
    from . import budget, db, freshness, mcp_lean, mcp_server as ms
    try:
        freshness.refresh(root)
    except Exception:  # noqa: BLE001 - a stale graph still ranks well
        pass
    conn = db.connect(root)
    try:
        ms.LEDGER = budget.Ledger()
        text = mcp_lean.find_text(root, conn, prompt[:PROMPT_CHARS])
    finally:
        conn.close()
    if not text or text.startswith(("No match", "Everything matching")):
        return ""
    return HEADER + text


def main(stdin=None, stdout=None) -> None:
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    try:
        data = json.loads(stdin.read() or "{}")
        prompt = (data.get("prompt") or "").strip()
        root = Path(data.get("cwd") or ".").resolve()
        from . import db
        if not prompt or not db.db_path(root).exists():
            return
        out = context_for(root, prompt)
        if out:
            stdout.write(out + "\n")
    except Exception as e:  # noqa: BLE001 - never break the user's request
        print(f"[codeorbit-hook] {e!r}", file=sys.stderr)
