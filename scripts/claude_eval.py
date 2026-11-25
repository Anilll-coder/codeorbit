"""Claude Code as the agent: the same issues, with and without CodeOrbit.

The frontier-model half of the pre-registered agent test (bench/PREREGISTRATION.md).
Each issue is given to Claude Code twice, non-interactively (`claude -p`), each
time in a fresh session in the repository at the issue's commit:

  without  Claude Code's read-only tools: Read, Grep, Glob
  with     the same, plus CodeOrbit's MCP server (explore, search, node, outline,
           impact; the server's instructions reach the model through MCP)

Claude must not edit or run anything; it ends its reply with ANSWER: and up to
five locations. Issues, gold targets and scoring are exactly those of
scripts/agent_eval.py, and the rows it writes are in the same format, so

    python scripts/claude_eval.py run --split verified --exclude-split lite --sample 100 --seed 3 --embed
    python scripts/agent_eval.py report bench/agent_runs/<run>

reports both models the same way. Authentication is Claude Code's own: on a
server, a token from `claude setup-token` in CLAUDE_CODE_OAUTH_TOKEN. This
script never reads or prints it.

Usage limits: when Claude Code reports a usage or rate limit, the run stops
cleanly without recording that issue; `--resume` continues from there.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

import agent_eval as ae                         # noqa: E402
import benchmark as bench                       # noqa: E402
import swebench as swe                          # noqa: E402
from codeorbit import db                        # noqa: E402
from codeorbit.indexer import index_project     # noqa: E402
from codeorbit.resolve import resolve_project   # noqa: E402

CONDITIONS = ("without", "with")
BASE_TOOLS = ["Read", "Grep", "Glob"]
CO_TOOLS = [f"mcp__codeorbit__{n}" for n in ae.CO_PARAMS]          # the same five
CO_HIDDEN = [f"mcp__codeorbit__codeorbit_{n}" for n in ("path", "audit", "overview")]
SESSION_TIMEOUT = 30 * 60
LIMIT_WORDS = re.compile(r"usage limit|rate limit|limit reached|quota|overloaded|"
                         r"too many requests|credit balance", re.I)

PROMPT = """\
You are localising a bug in the repository in the current directory. Read the \
GitHub issue below and find the functions or methods that must change to fix it. \
Do not edit any file and do not run any code.

When you are done, end your reply with a line containing only ANSWER: followed by \
up to 5 locations, one per line, most likely first, each written as \
<file path>:<function> or <file path>:<Class>.<method>, with the file path relative \
to the repository root.

Issue:
{issue}
"""


def claude_bin(path: str | None) -> str:
    b = path or shutil.which("claude")
    if not b:
        sys.exit("claude not found - install Claude Code first")
    return b


def mcp_command() -> list[str]:
    exe = shutil.which("codeorbit")
    if exe:
        return [exe]
    return [sys.executable, "-c", "import sys; from codeorbit.entry import main; "
            "sys.argv[0] = 'codeorbit'; main()"]


def hide_index(dest: Path) -> None:
    """Keep .codeorbit/ out of Grep and Glob in both conditions: ripgrep honours
    .git/info/exclude, so the index never shows up as a file to search."""
    ex = dest / ".git" / "info" / "exclude"
    ex.parent.mkdir(parents=True, exist_ok=True)
    text = ex.read_text(encoding="utf-8") if ex.exists() else ""
    if ".codeorbit/" not in text:
        ex.write_text(text + ("\n" if text and not text.endswith("\n") else "") + ".codeorbit/\n",
                      encoding="utf-8")


def parse_answer(text: str) -> list[str] | None:
    """Locations after the last ANSWER: line, or None if there is none."""
    if not text:
        return None
    m = None
    for m in re.finditer(r"(?im)^\s*[*_#\s]*ANSWER[*_\s]*:[*_\s]*(.*)$", text):
        pass
    if m is None:
        return None
    tail = (m.group(1) + "\n" + text[m.end():]).strip()
    locs = []
    for line in tail.splitlines():
        line = re.sub(r"^\s*(?:[-*]|\d+[.)])\s*", "", line).strip().strip("`")
        if line:
            locs.append(line)
    return locs[:ae.MAX_ANSWERS] or None


def session(binary: str, model: str, cond: str, prompt: str, dest: Path, cfg: Path,
            max_turns: int, count) -> dict:
    """One fresh Claude Code session; returns the episode record."""
    cmd = [binary, "-p", prompt, "--model", model, "--max-turns", str(max_turns),
           "--output-format", "stream-json", "--verbose", "--no-session-persistence",
           "--strict-mcp-config", "--tools", ",".join(BASE_TOOLS)]
    allowed = list(BASE_TOOLS)
    if cond == "with":
        cmd += ["--mcp-config", str(cfg), "--disallowedTools", ",".join(CO_HIDDEN)]
        allowed += CO_TOOLS
    cmd += ["--allowedTools", ",".join(allowed)]
    t0 = time.perf_counter()
    try:
        p = subprocess.run(cmd, cwd=dest, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=SESSION_TIMEOUT)
        out = p.stdout
    except subprocess.TimeoutExpired as e:
        out = e.stdout or ""
        if isinstance(out, bytes):
            out = out.decode("utf-8", "replace")
    seconds = round(time.perf_counter() - t0, 1)

    return parse_stream(out, seconds, count)


def parse_stream(out: str, seconds: float, count) -> dict:
    """The episode record from Claude Code's stream-json output."""
    tools: dict[str, int] = {}
    tool_tokens = 0
    peak = 0
    final, texts = None, []
    for line in out.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("type") == "assistant":
            msg = ev.get("message", {})
            u = msg.get("usage") or {}
            peak = max(peak, u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0)
                       + u.get("cache_creation_input_tokens", 0))
            for c in msg.get("content", []):
                if c.get("type") == "tool_use":
                    name = c.get("name", "?").replace("mcp__codeorbit__", "")
                    tools[name] = tools.get(name, 0) + 1
                elif c.get("type") == "text":
                    texts.append(c.get("text", ""))
        elif ev.get("type") == "user":
            for c in ev.get("message", {}).get("content", []):
                if isinstance(c, dict) and c.get("type") == "tool_result":
                    body = c.get("content")
                    if isinstance(body, list):
                        body = "".join(x.get("text", "") for x in body if isinstance(x, dict))
                    tool_tokens += count(str(body or ""))
        elif ev.get("type") == "result":
            final = ev
    if final is None:
        return {"error": "no result event (timeout or crash)", "seconds": seconds}
    if final.get("is_error") and not texts:
        return {"error": str(final.get("result", ""))[:300], "seconds": seconds}

    u = final.get("usage") or {}
    text = final.get("result") if isinstance(final.get("result"), str) else "\n".join(texts)
    answer = parse_answer(text) or parse_answer("\n".join(texts))
    return {
        "answer": answer, "answered": bool(answer), "malformed": 0,
        "turns": final.get("num_turns", 0),
        # Billed input: fresh + written to cache + read from cache, every turn.
        "input_tokens": u.get("input_tokens", 0) + u.get("cache_creation_input_tokens", 0)
        + u.get("cache_read_input_tokens", 0),
        "input_tokens_uncached": u.get("input_tokens", 0),
        "cache_read_tokens": u.get("cache_read_input_tokens", 0),
        "output_tokens": u.get("output_tokens", 0),
        "tool_tokens": tool_tokens, "peak_context": peak, "seconds": seconds,
        "tool_calls": tools, "stop": final.get("terminal_reason") or final.get("subtype"),
        "models": sorted((final.get("modelUsage") or {}).keys()),
        "cost_usd": final.get("total_cost_usd"),
        "final_text": (text or "")[-1500:],
    }


def run(args) -> None:
    binary = claude_bin(args.claude)
    tok_name, count, tok_exact = bench.make_tokenizer(args.tokenizer)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    if args.resume:
        out = Path(args.resume)
        meta = json.loads((out / "meta.json").read_text(encoding="utf-8"))
        sel = meta["dataset"]["selection"]
        for k in ("split", "repos", "instances", "limit", "sample", "seed", "exclude_split"):
            setattr(args, k, sel.get(k))
        args.model, args.embed, args.max_turns = (meta["run"]["model"], meta["run"]["embed"],
                                                  meta["run"]["max_turns"])
        rows = [json.loads(l) for l in (out / "runs.jsonl").read_text(encoding="utf-8").splitlines() if l]
        done = {(r["question_id"], r["condition"]) for r in rows}
        print(f"resuming {out}: {len(done)} session(s) already done")
    else:
        done = set()

    tasks = swe.load_tasks(args.split)
    if args.exclude_split:
        used = {t["instance_id"] for t in swe.load_tasks(args.exclude_split)}
        tasks = [t for t in tasks if t["instance_id"] not in used]
    tasks = swe.select(tasks, args.repos, args.instances, args.limit, args.sample, args.seed)
    if not tasks:
        sys.exit("no tasks selected")

    version = subprocess.run([binary, "--version"], capture_output=True, text=True).stdout.strip()
    if not args.resume:
        out = Path(args.out) / f"{stamp}-claude-{re.sub(r'[^A-Za-z0-9.]+', '-', args.model)}"
        out.mkdir(parents=True, exist_ok=True)
        meta = {
            "dataset": {"name": f"swebench-{args.split}", "source": swe.SPLITS[args.split],
                        "sha256": hashlib.sha256((swe.CACHE / f"{args.split}.jsonl")
                                                 .read_bytes()).hexdigest(),
                        "selection": {"split": args.split, "repos": args.repos,
                                      "instances": args.instances, "limit": args.limit,
                                      "sample": args.sample, "seed": args.seed,
                                      "exclude_split": args.exclude_split}},
            "codeorbit": {"commit": bench._git("rev-parse", "HEAD", cwd=REPO),
                          "dirty": bool(bench._git("status", "--porcelain",
                                                   "--untracked-files=no", cwd=REPO))},
            "run": {"utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "agent": "claude-code", "claude_code_version": version,
                    "model": args.model, "conditions": list(CONDITIONS),
                    "max_turns": args.max_turns, "embed": args.embed,
                    "base_tools": BASE_TOOLS, "codeorbit_tools": CO_TOOLS,
                    "tokenizer_for_tool_tokens": tok_name, "tasks_selected": len(tasks),
                    "complete": False, "models_seen": []},
            "environment": {"python": sys.version.split()[0], "platform": platform.platform(),
                            "packages": bench.package_versions()},
        }
        (out / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")

    cfg_dir = out / "_mcp"
    cfg_dir.mkdir(exist_ok=True)
    log = (out / "runs.jsonl").open("a", encoding="utf-8")
    t_start, stopped, reason = time.perf_counter(), False, ""
    seen_models = set(meta["run"].get("models_seen", []))
    for n, task in enumerate(tasks, 1):
        iid = task["instance_id"]
        todo = [c for c in CONDITIONS if (iid, c) not in done]
        if not todo:
            continue
        if args.max_hours and time.perf_counter() - t_start > args.max_hours * 3600:
            stopped, reason = True, f"--max-hours {args.max_hours}"
            break
        owner, name = task["repo"].split("/")
        dest = Path(args.checkouts) / f"{owner}__{name}"
        print(f"[{n}/{len(tasks)}] {iid}", flush=True)
        try:
            with swe.task_deadline(3600):
                swe.checkout(f"https://github.com/{task['repo']}", task["base_commit"], dest)
                index_project(dest)
                resolve_project(dest)
                conn = db.connect(dest)
                if args.embed:
                    from codeorbit import semantic
                    semantic.build(conn, dest)
        except Exception as e:      # noqa: BLE001 - reported, never silently dropped
            print(f"    skipped (setup): {type(e).__name__}: {e}", flush=True)
            continue
        hide_index(dest)
        targets, files = swe.gold_targets(conn, task["patch"])
        tfiles = ae.target_files(conn, targets)
        conn.close()
        if not files:
            print("    skipped: the fix touches no indexed file", flush=True)
            continue
        cfg = cfg_dir / f"{owner}__{name}.json"
        cfg.write_text(json.dumps({"mcpServers": {"codeorbit": {
            "command": mcp_command()[0], "args": mcp_command()[1:] + ["mcp", "--path", str(dest)]}}}),
            encoding="utf-8")
        prompt = PROMPT.format(issue=task["problem_statement"][:swe.QUESTION_CHARS])
        kind = swe.question_kind(task["problem_statement"], targets)
        order = todo if n % 2 else todo[::-1]
        marks = []
        for cond in order:
            rec = session(binary, args.model, cond, prompt, dest, cfg, args.max_turns, count)
            if "error" in rec:
                if LIMIT_WORDS.search(rec["error"]) or "auth" in rec["error"].lower():
                    stopped, reason = True, rec["error"]
                    break
                print(f"    {cond}: error, recorded as no answer: {rec['error']}", flush=True)
                rec = {"answer": None, "answered": False, "malformed": 0, "turns": 0,
                       "input_tokens": 0, "output_tokens": 0, "tool_tokens": 0,
                       "peak_context": 0, "seconds": rec["seconds"], "tool_calls": {},
                       "stop": "error: " + rec["error"][:120]}
            seen_models.update(rec.get("models", []))
            row = {"question_id": iid, "repo": task["repo"], "kind": kind, "condition": cond,
                   "targets": targets, "files": files, "target_files": tfiles,
                   **rec, **ae.score_answer(rec["answer"], targets, tfiles, files)}
            log.write(json.dumps(row) + "\n")
            log.flush()
            marks.append(f"{cond}: {'fn' if row['fn_hit'] else 'file' if row['file_hit'] else 'miss'}"
                         f" {row['turns']}t {row['input_tokens']}tok {row['seconds']}s")
        print("    " + " | ".join(marks), flush=True)
        if stopped:
            break
    log.close()
    if reason:
        print(f"\nstopped: {reason}\nresume with: python scripts/claude_eval.py run --resume {out}")
    meta["run"]["complete"] = not stopped
    meta["run"]["models_seen"] = sorted(seen_models)
    meta["run"]["wall_s"] = round(meta["run"].get("wall_s", 0) + time.perf_counter() - t_start, 1)
    (out / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    shutil.rmtree(cfg_dir, ignore_errors=True)
    ae.report_runs([out])


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--model", default="sonnet", help="Claude Code model alias or ID")
    r.add_argument("--split", default="verified", choices=list(swe.SPLITS))
    r.add_argument("--exclude-split", choices=list(swe.SPLITS))
    r.add_argument("--repos")
    r.add_argument("--instances")
    r.add_argument("--limit", type=int)
    r.add_argument("--sample", type=int)
    r.add_argument("--seed", type=int, default=0)
    r.add_argument("--max-turns", type=int, default=ae.MAX_TURNS)
    r.add_argument("--embed", action="store_true")
    r.add_argument("--tokenizer", default="hf:microsoft/Phi-4-mini-instruct")
    r.add_argument("--checkouts", default=str(swe.CHECKOUTS))
    r.add_argument("--out", default=str(REPO / "bench" / "agent_runs"))
    r.add_argument("--max-hours", type=float)
    r.add_argument("--resume")
    r.add_argument("--claude", help="path to the claude binary")
    args = ap.parse_args()
    run(args)


if __name__ == "__main__":
    main()
