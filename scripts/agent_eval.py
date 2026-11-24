"""Real-agent test: does adding CodeOrbit's tools help a model localise real bugs?

A local tool-calling model (through Ollama) is given a SWE-bench issue and the
repository at the issue's commit, and must name the functions to change. Each
task is run twice, from a fresh conversation, with the same model, prompt and
limits:

  without  the tools coding agents use today: grep, read_file, list_dir
  with     the same three, plus CodeOrbit's MCP tools (explore, search, node,
           outline, impact) and the MCP server's instructions

So "with" is exactly what a user gets by adding the CodeOrbit MCP server to an
agent. The CodeOrbit tools are the shipped mcp_server functions, called in
process rather than over stdio - the same code and the same text.

Measured per run: whether the answer contains a function the real fix changed
(and its file), the tokens the conversation cost (input re-sent every turn, as
an API bills it, plus output), the tokens the tools returned, turns and time.
`report` pairs the two conditions task by task: exact McNemar test on success,
paired bootstrap confidence intervals on the differences.

    python scripts/agent_eval.py run --model qwen3:8b --exclude-sample 60 --embed
    python scripts/agent_eval.py run --resume bench/agent_runs/<run>
    python scripts/agent_eval.py report bench/agent_runs/<run> [<run> ...]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import platform
import random
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

import benchmark as bench                       # noqa: E402
import swebench as swe                          # noqa: E402

CONDITIONS = ("without", "with")
MAX_TURNS = 20              # model turns per run; the last one must answer
NUM_CTX = 16384             # context window asked of Ollama
CTX_RESERVE = 2500          # tokens kept free for the answer and one more tool result
TOOL_CHARS = 12_000         # cap on one tool result, the same for every tool
GREP_MATCHES = 60
READ_LINES = 200
MAX_ANSWERS = 5

SYSTEM = """\
You are a software engineer localising a bug. You are given a GitHub issue and
the repository it was filed against, checked out at that commit. Find the
functions or methods that must be changed to fix the issue.

Use the tools to explore the code. You cannot run code. When you are confident,
call finish with up to 5 locations, most likely first, each written as
<file path>:<function> or <file path>:<Class>.<method>, using the file paths
exactly as the tools show them. Name functions and methods, not just files.
"""

NUDGE = ("Use one of the tools to keep exploring, or call finish with the "
         "locations.")
LAST_TURN = ("You are out of turns. Call finish now with your best locations, "
             "most likely first.")
FULL = ("The conversation is close to the context limit. Call finish now with "
        "your best locations, most likely first.")


# --------------------------------------------------------------- tools

def _fn(name: str, description: str, props: dict, required: list[str]) -> dict:
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": props, "required": required}}}


_STR = {"type": "string"}
_INT = {"type": "integer"}

BASE_TOOLS = [
    _fn("grep", "Search the repository's files for a regular expression (git grep). "
        f"Returns up to {GREP_MATCHES} matching lines as path:line:text.",
        {"pattern": {**_STR, "description": "regular expression"},
         "path": {**_STR, "description": "directory or file to search; default: whole repository"},
         "ignore_case": {"type": "boolean"}}, ["pattern"]),
    _fn("read_file", f"Read lines of a file, numbered. At most {READ_LINES} lines per call.",
        {"path": _STR, "start_line": _INT, "end_line": _INT}, ["path"]),
    _fn("list_dir", "List the files and directories in a directory.",
        {"path": {**_STR, "description": "default: repository root"}}, []),
]

FINISH = _fn(
    "finish", "Give the final answer: the functions or methods to change.",
    {"locations": {"type": "array", "items": _STR,
                   "description": "up to 5, most likely first, as path/to/file.py:QualifiedName"}},
    ["locations"])

# The CodeOrbit tools exposed, with the parameters the MCP server publishes
# (project_path is supplied by the harness, as the server's working directory
# would be).
CO_PARAMS = {
    "codeorbit_explore": ({"query_text": _STR, "max_symbols": _INT,
                           "detail": {**_STR, "enum": ["auto", "full", "signatures"]}},
                          ["query_text"]),
    "codeorbit_search": ({"term": _STR, "limit": _INT}, ["term"]),
    "codeorbit_node": ({"name": _STR,
                        "detail": {**_STR, "enum": ["auto", "full", "signatures"]}},
                       ["name"]),
    "codeorbit_outline": ({"path": _STR}, ["path"]),
    "codeorbit_impact": ({"name": _STR, "depth": _INT}, ["name"]),
}


def codeorbit_tools() -> tuple[list[dict], dict, str]:
    """(schemas, callables, server instructions), from the shipped MCP server."""
    from codeorbit import mcp_server as mcp
    schemas, fns = [], {}
    for name, (props, req) in CO_PARAMS.items():
        fn = getattr(mcp, name)
        doc = re.sub(r"\s+", " ", (fn.__doc__ or "").strip())
        schemas.append(_fn(name, doc, props, req))
        fns[name] = fn
    return schemas, fns, mcp.INSTRUCTIONS


class Workspace:
    """The base tools, confined to one checkout."""

    def __init__(self, root: Path):
        self.root = root.resolve()

    def _path(self, p: str | None) -> Path:
        p = (p or ".").strip().lstrip("/") or "."
        full = (self.root / p).resolve()
        if full != self.root and self.root not in full.parents:
            raise ValueError(f"{p} is outside the repository")
        return full

    def _rel(self, p: Path) -> str:
        return p.relative_to(self.root).as_posix() or "."

    def grep(self, pattern: str, path: str | None = None, ignore_case: bool = False) -> str:
        target = self._path(path)
        args = ["git", "-C", str(self.root), "grep", "-n", "-I", "--no-color"]
        if ignore_case:
            args.append("-i")
        for mode in ("-E", "-F"):       # a pattern that is not a valid regex: literal
            r = subprocess.run(args + [mode, "-e", pattern, "--", self._rel(target)],
                               capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=60)
            if r.returncode in (0, 1):
                break
        lines = r.stdout.splitlines()
        if not lines:
            return f"No matches for {pattern!r}."
        out = [ln if len(ln) <= 200 else ln[:200] + "..." for ln in lines[:GREP_MATCHES]]
        if len(lines) > GREP_MATCHES:
            out.append(f"[{len(lines) - GREP_MATCHES} more matches - narrow the "
                       "pattern or the path]")
        return "\n".join(out)

    def read_file(self, path: str, start_line: int = 1, end_line: int | None = None) -> str:
        full = self._path(path)
        if not full.is_file():
            return f"{path}: no such file."
        lines = full.read_text(encoding="utf-8", errors="replace").splitlines()
        start = max(1, int(start_line or 1))
        end = min(len(lines), int(end_line) if end_line else start + READ_LINES - 1,
                  start + READ_LINES - 1)
        body = "\n".join(f"{i:>5} {lines[i - 1]}" for i in range(start, end + 1))
        more = (f"\n[lines {start}-{end} of {len(lines)}; read on with "
                f"start_line={end + 1}]" if end < len(lines) else "")
        return f"{self._rel(full)} (lines {start}-{end} of {len(lines)})\n{body}{more}"

    def list_dir(self, path: str | None = None) -> str:
        full = self._path(path)
        if not full.is_dir():
            return f"{path}: no such directory."
        names = sorted((p.name + "/" if p.is_dir() else p.name) for p in full.iterdir()
                       if not p.name.startswith(".") or p.name in (".github",))
        extra = f"\n[{len(names) - 200} more]" if len(names) > 200 else ""
        return "\n".join(names[:200]) + extra


def call_tool(name: str, args: dict, ws: Workspace, co: dict | None) -> str:
    if not isinstance(args, dict):
        return "Tool arguments must be a JSON object."
    try:
        if name in ("grep", "read_file", "list_dir"):
            out = getattr(ws, name)(**args)
        elif co is not None and name in co:
            out = co[name](**args, project_path=str(ws.root))
        else:
            return f"There is no tool named {name}."
    except TypeError as e:
        return f"Bad arguments for {name}: {e}"
    except Exception as e:      # noqa: BLE001 - the model gets the error, the run goes on
        return f"{name} failed: {type(e).__name__}: {e}"
    if len(out) > TOOL_CHARS:
        out = out[:TOOL_CHARS] + "\n[output truncated]"
    return out


# --------------------------------------------------------------- the model

def think_setting(model: str):
    """Thinking models answer without a hidden chain of thought: same budget for all."""
    return False if model.split(":")[0] in ("qwen3", "deepseek-r1") else None


def chat(model: str, messages: list[dict], tools: list[dict], seed: int,
         url: str) -> dict:
    body = {"model": model, "messages": messages, "tools": tools, "stream": False,
            "options": {"num_ctx": NUM_CTX, "temperature": 0, "seed": seed,
                        "num_predict": 1024}}
    think = think_setting(model)
    if think is not None:
        body["think"] = think
    r = requests.post(f"{url}/api/chat", json=body, timeout=900)
    r.raise_for_status()
    return r.json()


def inline_calls(text: str) -> list[dict]:
    """Tool calls a model wrote as JSON in its text instead of as tool calls."""
    calls, dec, i = [], json.JSONDecoder(), 0
    while (i := text.find("{", i)) != -1:
        try:
            obj, end = dec.raw_decode(text, i)
        except ValueError:
            i += 1
            continue
        if isinstance(obj, dict) and isinstance(obj.get("name"), str):
            args = obj.get("arguments", obj.get("parameters", {}))
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    args = {}
            calls.append({"function": {"name": obj["name"], "arguments": args}})
        i = end
    return calls


def _calls(msg: dict) -> list[dict]:
    calls = msg.get("tool_calls") or inline_calls(msg.get("content") or "")
    out = []
    for c in calls:
        f = c.get("function", {})
        args = f.get("arguments", {})
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except ValueError:
                args = {}
        out.append((f.get("name", ""), args))
    return out


def answer_list(locs) -> list[str]:
    """finish's locations, also when a model sends the list as one JSON string."""
    if isinstance(locs, str):
        try:
            locs = json.loads(locs)
        except ValueError:
            locs = [x for x in re.split(r"[,\n]", locs) if x.strip()]
    if isinstance(locs, str):
        locs = [locs]
    out = []
    for x in locs or []:
        if isinstance(x, str) and x.strip().startswith("["):
            out += answer_list(x)
        else:
            out.append(str(x).strip())
    return [x for x in out if x][:MAX_ANSWERS]


def conversation_text(messages: list[dict], tools: list[dict]) -> str:
    """Everything sent to the model in one request, for counting tokens."""
    parts = [json.dumps(tools)]
    for m in messages:
        parts.append(m.get("content") or "")
        for c in m.get("tool_calls") or []:
            parts.append(json.dumps(c.get("function", {})))
    return "\n".join(parts)


def episode(model: str, condition: str, issue: str, ws: Workspace, count,
            seed: int, url: str, co_tools) -> dict:
    tools = list(BASE_TOOLS)
    system = SYSTEM
    co = None
    if condition == "with":
        from codeorbit import budget, mcp_server
        mcp_server.LEDGER = budget.Ledger()     # a fresh MCP session per run
        schemas, co, instructions = co_tools
        tools += schemas
        system = SYSTEM + "\nTools from the CodeOrbit MCP server are available. " \
                          "Its instructions:\n" + instructions
    tools.append(FINISH)
    messages = [{"role": "system", "content": system},
                {"role": "user", "content": f"Issue:\n\n{issue}"}]
    rec = {"turns": 0, "tool_calls": {}, "input_tokens": 0, "output_tokens": 0,
           "tool_tokens": 0, "peak_context": 0, "ollama_prompt": 0, "ollama_eval": 0,
           "answer": None, "stop": "turns", "malformed": 0}
    t0 = time.perf_counter()
    forced = False
    for turn in range(MAX_TURNS):
        ctx = count(conversation_text(messages, tools))
        if not forced and (turn == MAX_TURNS - 1 or ctx > NUM_CTX - CTX_RESERVE):
            messages.append({"role": "user",
                             "content": LAST_TURN if turn == MAX_TURNS - 1 else FULL})
            tools = [FINISH]            # one way out
            forced = True
            ctx = count(conversation_text(messages, tools))
        rec["input_tokens"] += ctx
        rec["peak_context"] = max(rec["peak_context"], ctx)
        resp = chat(model, messages, tools, seed, url)
        rec["turns"] += 1
        rec["ollama_prompt"] += resp.get("prompt_eval_count") or 0
        rec["ollama_eval"] += resp.get("eval_count") or 0
        msg = resp.get("message", {})
        calls = _calls(msg)
        rec["output_tokens"] += count((msg.get("content") or "") +
                                      "".join(json.dumps(a) + n for n, a in calls))
        messages.append({k: msg[k] for k in ("role", "content", "tool_calls") if k in msg})
        if not calls:
            rec["malformed"] += 1
            if forced:
                rec["stop"] = "no_answer"
                break
            messages.append({"role": "user", "content": NUDGE})
            continue
        done = False
        for name, args in calls:
            rec["tool_calls"][name] = rec["tool_calls"].get(name, 0) + 1
            if name == "finish":
                locs = args.get("locations") if isinstance(args, dict) else None
                rec["answer"] = answer_list(locs)
                rec["stop"] = "finish"
                done = True
                break
            out = call_tool(name, args, ws, co)
            rec["tool_tokens"] += count(out)
            messages.append({"role": "tool", "content": out, "tool_name": name})
        if done:
            break
        if forced:                      # it called a tool instead of finishing
            rec["stop"] = "no_answer"
            break
    rec["seconds"] = round(time.perf_counter() - t0, 1)
    return rec


# --------------------------------------------------------------- scoring

_EXTS = sorted({k[1:] for k in __import__("codeorbit.langs", fromlist=["SUFFIX"]).SUFFIX
                if k.startswith(".")}, key=len, reverse=True)
_LOC = re.compile(r"^(.+?\.(?:%s))(?:(?:::|:|#|\s+)(.*))?$" % "|".join(map(re.escape, _EXTS)))


def parse_location(s: str) -> tuple[str | None, str | None]:
    """'pkg/mod.py:Cls.meth' -> ('pkg/mod.py', 'Cls.meth'); tolerant of the variants."""
    s = s.strip().strip("`'\"").replace("\\", "/")
    path, name = None, s
    m = _LOC.match(s)
    if m:
        path, name = m.group(1), (m.group(2) or "").strip()
        path = path[2:] if path.startswith("./") else path.lstrip("/")
    # The qualified name at the front; whatever follows it - "()", a line
    # number, a pasted line of code - is not part of it.
    m = re.match(r"[A-Za-z_$][\w$]*(?:(?:\.|::|#)[A-Za-z_$][\w$]*)*", (name or "").strip(" .:"))
    return path, (m.group(0).replace("::", ".").replace("#", ".") if m else None)


def _path_ok(pred: str | None, gold: str) -> bool:
    return pred is None or pred == gold or gold.endswith("/" + pred) or pred.endswith("/" + gold)


def fn_match(loc: str, target: str, target_file: str) -> bool:
    """Does a predicted location name this gold function?

    The name must match the qualified name's tail - 'Session.request' or
    'request' for requests.sessions.Session.request - and a path, if given,
    must be the gold file. Gold qnames are module-qualified, which no one writes.
    """
    path, name = parse_location(loc)
    if not name or not _path_ok(path, target_file):
        return False
    name = name.replace("::", ".").replace("/", ".")
    return target == name or target.endswith("." + name)


def file_match(loc: str, files: list[str]) -> bool:
    path, _ = parse_location(loc)
    return path is not None and any(_path_ok(path, f) for f in files)


def score_answer(answer: list[str] | None, targets: list[str], target_files: dict,
                 files: list[str]) -> dict:
    answer = answer or []

    def hits(loc):
        return [t for t in targets if fn_match(loc, t, target_files.get(t, ""))]

    first = next((i for i, a in enumerate(answer, 1) if hits(a)), None)
    found = {t for a in answer for t in hits(a)}
    ffirst = next((i for i, a in enumerate(answer, 1) if file_match(a, files)), None)
    return {"fn_rank": first, "fn_hit": first is not None, "fn_hit1": first == 1,
            "fn_all": bool(targets) and found == set(targets),
            "file_rank": ffirst, "file_hit": ffirst is not None, "file_hit1": ffirst == 1,
            "answered": bool(answer)}


def target_files(conn, targets: list[str]) -> dict:
    out = {}
    for t in targets:
        row = conn.execute("SELECT f.path FROM nodes n JOIN files f ON f.id = n.file_id "
                           "WHERE n.qname = ? LIMIT 1", (t,)).fetchone()
        out[t] = row["path"] if row else ""
    return out


# --------------------------------------------------------------- run

def run(args) -> None:
    from codeorbit import db, llm
    from codeorbit.indexer import index_project
    from codeorbit.resolve import resolve_project

    url = args.ollama.rstrip("/")
    llm.OLLAMA_URL = url                        # embeddings from the same server
    try:
        requests.get(f"{url}/api/version", timeout=5).raise_for_status()
    except Exception as e:      # noqa: BLE001
        sys.exit(f"Ollama is not answering at {url}: {type(e).__name__}")

    if args.resume:
        out = Path(args.resume)
        meta = json.loads((out / "meta.json").read_text(encoding="utf-8"))
        r = meta["run"]
        args.model, args.seed_runs, args.embed = r["model"], r["seed"], r["embed"]
        sel = meta["dataset"]["selection"]
        for k in ("repos", "instances", "limit", "sample", "seed", "exclude_sample",
                  "exclude_seed"):
            setattr(args, k, sel[k])
        args.shard = sel.get("shard")
        done = {(x["question_id"], x["condition"]) for x in
                map(json.loads, (out / "runs.jsonl").read_text(encoding="utf-8").splitlines())}
        print(f"resuming {out}: {len(done)} run(s) already done")
    else:
        done = set()

    all_tasks = swe.load_tasks(args.split)
    if args.exclude_sample:
        seen = {t["instance_id"] for t in swe.select(all_tasks, None, None, None,
                                                     args.exclude_sample, args.exclude_seed)}
        all_tasks = [t for t in all_tasks if t["instance_id"] not in seen]
    tasks = swe.select(all_tasks, args.repos, args.instances, args.limit,
                       args.sample, args.seed)
    if args.shard:
        # `--shard 1/2`: every 2nd task from the 1st, so two processes on two
        # GPUs split one selection exactly; `report` takes both run folders.
        k, n = (int(x) for x in args.shard.split("/"))
        tasks = tasks[k - 1::n]
    if not tasks:
        sys.exit("no tasks selected")

    try:                                        # the model is there and can call tools
        chat(args.model, [{"role": "user", "content": "Say hi."}], [FINISH],
             args.seed_runs, url)
    except Exception as e:      # noqa: BLE001
        sys.exit(f"model {args.model} failed at {url}: {e}\n"
                 f"pull it first: ollama pull {args.model}")
    if args.embed:
        try:
            llm.embed(["probe"])
        except Exception as e:  # noqa: BLE001
            sys.exit(f"--embed needs {llm.EMBED_MODEL} at {url}: {type(e).__name__}")

    tok_name, count, tok_exact = bench.make_tokenizer(args.tokenizer)
    co_tools = codeorbit_tools()

    if not args.resume:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        safe = re.sub(r"[^A-Za-z0-9.]+", "-", args.model)
        if args.shard:
            safe += "-shard" + args.shard.replace("/", "of")
        out = Path(args.out) / f"{stamp}-agent-{safe}"
        out.mkdir(parents=True, exist_ok=True)
        meta = {
            "dataset": {"name": f"swebench-{args.split}", "source": swe.SPLITS[args.split],
                        "sha256": hashlib.sha256((swe.CACHE / f"{args.split}.jsonl")
                                                 .read_bytes()).hexdigest(),
                        "selection": {"repos": args.repos, "instances": args.instances,
                                      "limit": args.limit, "sample": args.sample,
                                      "seed": args.seed,
                                      "exclude_sample": args.exclude_sample,
                                      "exclude_seed": args.exclude_seed,
                                      "shard": args.shard}},
            "codeorbit": {"commit": bench._git("rev-parse", "HEAD", cwd=REPO),
                          "dirty": bool(bench._git("status", "--porcelain",
                                                   "--untracked-files=no", cwd=REPO))},
            "run": {"utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "model": args.model, "conditions": list(CONDITIONS),
                    "seed": args.seed_runs, "temperature": 0, "think": think_setting(args.model),
                    "num_ctx": NUM_CTX, "max_turns": MAX_TURNS, "tool_chars": TOOL_CHARS,
                    "grep_matches": GREP_MATCHES, "read_lines": READ_LINES,
                    "question_chars": swe.QUESTION_CHARS, "embed": args.embed,
                    "codeorbit_tools": list(CO_PARAMS), "tokenizer": tok_name,
                    "tokenizer_exact": tok_exact, "tasks_selected": len(tasks),
                    "complete": False},
            "environment": {"python": sys.version.split()[0],
                            "platform": platform.platform(),
                            "packages": bench.package_versions(),
                            "ollama": requests.get(f"{url}/api/version", timeout=5).json()},
        }
        (out / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")

    log = (out / "runs.jsonl").open("a", encoding="utf-8")
    t_start = time.perf_counter()
    stopped = False
    for n, task in enumerate(tasks, 1):
        iid = task["instance_id"]
        todo = [c for c in CONDITIONS if (iid, c) not in done]
        if not todo:
            continue
        if args.max_hours and time.perf_counter() - t_start > args.max_hours * 3600:
            print(f"stopping after {args.max_hours} h (--max-hours); resume to continue")
            stopped = True
            break
        owner, name = task["repo"].split("/")
        dest = Path(args.checkouts) / f"{owner}__{name}"
        print(f"[{n}/{len(tasks)}] {iid}", flush=True)
        try:
            swe.checkout(f"https://github.com/{task['repo']}", task["base_commit"], dest)
            index_project(dest)
            resolve_project(dest)
            conn = db.connect(dest)
            if args.embed:
                from codeorbit import semantic
                semantic.build(conn, dest)
        except Exception as e:      # noqa: BLE001
            print(f"    skipped: {type(e).__name__}: {e}")
            continue
        targets, files = swe.gold_targets(conn, task["patch"])
        tfiles = target_files(conn, targets)
        conn.close()
        if not files:
            print("    skipped: the fix touches no indexed file")
            continue
        issue = task["problem_statement"][:swe.QUESTION_CHARS]
        kind = swe.question_kind(task["problem_statement"], targets)
        ws = Workspace(dest)
        # Alternate which condition goes first, so neither always gets a warm cache.
        order = todo if n % 2 else todo[::-1]
        marks = []
        for cond in order:
            try:
                rec = episode(args.model, cond, issue, ws, count, args.seed_runs, url,
                              co_tools)
            except requests.RequestException as e:
                log.close()
                sys.exit(f"Ollama failed mid-run ({type(e).__name__}: {e}); "
                         f"resume with --resume {out}")
            row = {"question_id": iid, "repo": task["repo"], "kind": kind,
                   "condition": cond, "targets": targets, "files": files,
                   "target_files": tfiles,
                   **rec, **score_answer(rec["answer"], targets, tfiles, files)}
            log.write(json.dumps(row) + "\n")
            log.flush()
            marks.append(f"{cond}: {'fn' if row['fn_hit'] else 'file' if row['file_hit'] else 'miss'}"
                         f" {row['turns']}t {row['input_tokens']}tok {rec['seconds']}s")
        print("    " + " | ".join(marks), flush=True)
    log.close()

    meta["run"]["complete"] = not stopped
    meta["run"]["wall_s"] = round(meta["run"].get("wall_s", 0)
                                  + time.perf_counter() - t_start, 1)
    (out / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    print(f"\nwrote {out}")
    report_runs([out])


# --------------------------------------------------------------- statistics

def mcnemar(b: int, c: int) -> float:
    """Exact two-sided McNemar p-value from the discordant counts."""
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    p = 2 * sum(math.comb(n, i) for i in range(k + 1)) / 2 ** n
    return min(1.0, p)


def boot_ci(diffs: list[float], reps: int = 10_000, seed: int = 0) -> tuple[float, float]:
    """95% percentile bootstrap CI of the mean of paired differences."""
    if not diffs:
        return (float("nan"), float("nan"))
    rng = random.Random(seed)
    n = len(diffs)
    means = sorted(sum(diffs[rng.randrange(n)] for _ in range(n)) / n for _ in range(reps))
    return means[int(0.025 * reps)], means[int(0.975 * reps) - 1]


def median(xs: list[float]) -> float:
    s = sorted(xs)
    n = len(s)
    return float("nan") if not n else (s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2)


def paired(rows: list[dict]) -> list[tuple[dict, dict]]:
    by = {}
    for r in rows:
        by.setdefault(r["question_id"], {})[r["condition"]] = r
    return [(d["without"], d["with"]) for d in by.values() if len(d) == 2]


def compare(pairs: list[tuple[dict, dict]]) -> dict:
    """With vs without, on the tasks both finished. Function metrics on tasks with a function target."""
    fn_pairs = [(a, b) for a, b in pairs if a["targets"]]
    out = {"tasks": len(pairs), "fn_tasks": len(fn_pairs)}
    for key, ps in (("fn_hit", fn_pairs), ("fn_hit1", fn_pairs), ("fn_all", fn_pairs),
                    ("file_hit", pairs), ("file_hit1", pairs)):
        wo = sum(a[key] for a, _ in ps)
        wi = sum(b[key] for _, b in ps)
        only_with = sum(b[key] and not a[key] for a, b in ps)
        only_without = sum(a[key] and not b[key] for a, b in ps)
        n = len(ps) or 1
        out[key] = {"without": wo / n, "with": wi / n, "only_with": only_with,
                    "only_without": only_without, "p": mcnemar(only_with, only_without),
                    "ci_diff": boot_ci([float(b[key]) - float(a[key]) for a, b in ps])}
    for key in ("input_tokens", "output_tokens", "tool_tokens", "turns", "seconds"):
        wo = [a[key] for a, _ in pairs]
        wi = [b[key] for _, b in pairs]
        out[key] = {"without_mean": sum(wo) / len(wo), "with_mean": sum(wi) / len(wi),
                    "without_median": median(wo), "with_median": median(wi),
                    "with_lower": sum(b < a for a, b in zip(wo, wi)),
                    "ci_diff": boot_ci([b - a for a, b in zip(wo, wi)])}
    # Cost of a success: total tokens over the tasks each condition localised.
    for cond, i in (("without", 0), ("with", 1)):
        ok = sum(p[i]["fn_hit"] for p in fn_pairs)
        tok = sum(p[i]["input_tokens"] + p[i]["output_tokens"] for p in fn_pairs)
        out.setdefault("tokens_per_success", {})[cond] = tok / ok if ok else None
    out["answered"] = {c: sum(p[i]["answered"] for p in pairs) / (len(pairs) or 1)
                       for c, i in (("without", 0), ("with", 1))}
    used = {}
    for _, b in pairs:
        for t, k in b["tool_calls"].items():
            used[t] = used.get(t, 0) + k
    out["with_tool_calls"] = used
    return out


def _pct(x):
    return f"{x:.0%}"


def print_compare(name: str, c: dict) -> None:
    print(f"\n{name}: {c['tasks']} tasks with both runs ({c['fn_tasks']} with a function target)")
    print(f"{'':<22}{'without':>9}{'with':>9}   {'diff 95% CI':<18}{'with only':>10}"
          f"{'w/o only':>10}{'p':>8}")
    labels = {"fn_hit": "function found (top 5)", "fn_hit1": "function first",
              "fn_all": "all functions found", "file_hit": "file found",
              "file_hit1": "file first"}
    for key, label in labels.items():
        m = c[key]
        lo, hi = m["ci_diff"]
        print(f"{label:<22}{_pct(m['without']):>9}{_pct(m['with']):>9}   "
              f"{f'{lo:+.0%} to {hi:+.0%}':<18}{m['only_with']:>10}{m['only_without']:>10}"
              f"{m['p']:>8.3f}")
    print()
    print(f"{'':<22}{'without':>9}{'with':>9}   {'mean diff 95% CI':<22}{'with lower':>11}")
    for key, label in (("input_tokens", "input tokens"), ("output_tokens", "output tokens"),
                       ("tool_tokens", "tool output tokens"), ("turns", "turns"),
                       ("seconds", "seconds")):
        m = c[key]
        lo, hi = m["ci_diff"]
        print(f"{label + ' (median)':<22}{m['without_median']:>9,.0f}{m['with_median']:>9,.0f}   "
              f"{f'{lo:+,.0f} to {hi:+,.0f}':<22}{m['with_lower']:>7}/{c['tasks']}")
    tps = c["tokens_per_success"]
    fmt = lambda v: f"{v:,.0f}" if v else "-"      # noqa: E731
    print(f"{'tokens per success':<22}{fmt(tps['without']):>9}{fmt(tps['with']):>9}")
    print(f"{'gave an answer':<22}{_pct(c['answered']['without']):>9}"
          f"{_pct(c['answered']['with']):>9}")
    print("tool calls (with): " + ", ".join(f"{k} {v}" for k, v in
                                            sorted(c["with_tool_calls"].items(),
                                                   key=lambda kv: -kv[1])))


def _load(run: Path) -> tuple[dict, list[dict]]:
    meta = json.loads((run / "meta.json").read_text(encoding="utf-8"))
    rows = [json.loads(l) for l in
            (run / "runs.jsonl").read_text(encoding="utf-8").splitlines() if l]
    for r in rows:              # scored again from the saved answer: scoring fixes apply
        tf = r.get("target_files") or {t: r["files"][0] for t in r["targets"]
                                       if len(r["files"]) == 1}
        r.update(score_answer(r["answer"], r["targets"], tf, r["files"]))
    return meta, rows


def report_runs(runs: list[Path], pool: bool = False) -> None:
    loaded = [(run, *_load(run)) for run in runs]
    if pool:
        # Shards of one selection (`--shard K/N`): one model, disjoint tasks,
        # so the paired test runs over all of them together.
        models = {m["run"]["model"] for _, m, _ in loaded}
        if len(models) != 1:
            sys.exit(f"--pool needs runs of one model, got {sorted(models)}")
        meta = dict(loaded[0][1])
        meta["run"] = dict(meta["run"], complete=all(m["run"].get("complete")
                                                     for _, m, _ in loaded))
        out = runs[0].parent / f"pooled-{runs[0].name}"
        out.mkdir(exist_ok=True)
        loaded = [(out, meta, [r for _, _, rows in loaded for r in rows])]
    for run, meta, rows in loaded:
        pairs = paired(rows)
        if not pairs:
            print(f"{run}: no task has both runs yet")
            continue
        state = "" if meta["run"].get("complete") else "  [incomplete]"
        print(f"\n=== {meta['run']['model']}  ({run.name}){state}")
        res = {"all": compare(pairs)}
        print_compare("all tasks", res["all"])
        for kind in ("named", "unnamed"):
            sub = [p for p in pairs if p[0]["kind"] == kind]
            if sub:
                res[kind] = compare(sub)
                print_compare(f"{kind} issues", res[kind])
        (run / "summary.json").write_text(json.dumps(res, indent=2), encoding="utf-8")


# --------------------------------------------------------------- cli

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="run the agent with and without CodeOrbit")
    r.add_argument("--model", default="qwen3:8b")
    r.add_argument("--split", default="lite", choices=list(swe.SPLITS))
    r.add_argument("--repos")
    r.add_argument("--instances")
    r.add_argument("--limit", type=int)
    r.add_argument("--sample", type=int)
    r.add_argument("--seed", type=int, default=0, help="task sample seed")
    r.add_argument("--exclude-sample", type=int, help="leave out the development sample")
    r.add_argument("--exclude-seed", type=int, default=0)
    r.add_argument("--shard", metavar="K/N", help="run the K-th of N interleaved slices")
    r.add_argument("--seed-runs", type=int, default=0, help="the model's sampling seed")
    r.add_argument("--embed", action="store_true",
                   help="embed each checkout, so CodeOrbit's explore uses meaning too")
    r.add_argument("--tokenizer", default="hf:microsoft/Phi-4-mini-instruct")
    r.add_argument("--ollama", default="http://127.0.0.1:11434")
    r.add_argument("--checkouts", default=str(swe.CHECKOUTS))
    r.add_argument("--out", default=str(REPO / "bench" / "agent_runs"))
    r.add_argument("--max-hours", type=float)
    r.add_argument("--resume")
    p = sub.add_parser("report", help="paired with/without comparison of finished runs")
    p.add_argument("runs", nargs="+")
    p.add_argument("--pool", action="store_true",
                   help="one result over several shard runs of the same model")
    args = ap.parse_args()
    if args.cmd == "run":
        run(args)
    else:
        report_runs([Path(x) for x in args.runs], pool=args.pool)


if __name__ == "__main__":
    main()
