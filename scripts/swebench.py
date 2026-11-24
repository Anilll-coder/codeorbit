"""Bug localisation on SWE-bench: does each retriever find the code a real fix changed?

Every SWE-bench task is a real GitHub issue, the commit it was filed against,
and the patch that fixed it. The functions that patch edits are the ground
truth - nobody here writes or labels it. The question is the issue text, as the
user wrote it.

Per task:
  1. check out the repository at the task's base commit (bench/repos/swe/...)
  2. index it (incremental - consecutive tasks on one repo re-parse only what
     changed) and resolve calls; embed if --embed
  3. map each patched line to the innermost function/method/class containing
     it at that commit - those qnames are the targets
  4. run every benchmark arm with the issue as the question

Scored at two levels, as localisation papers report them:
  function  rank of the first gold function in the top 10, and whether any
            gold function's name/definition is in the budgeted context
  file      rank of the first gold file, and whether it is in the context

A task whose patch only touches module-level code has no function target; it
counts for file-level scores and is excluded from function-level ones.

    python scripts/swebench.py download --split lite
    python scripts/swebench.py list --split lite
    python scripts/swebench.py run --split lite --repos psf/requests --limit 5
    python scripts/swebench.py run --split lite --sample 60 --seed 0 --embed
    python scripts/swebench.py run --split lite --exclude-sample 60 --exclude-seed 0 --embed
    python scripts/swebench.py run --split lite --resume bench/runs/<dir>
"""
from __future__ import annotations

import argparse
import hashlib
import json
import platform
import random
import re
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import requests

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

import benchmark as bench                       # noqa: E402
from bench_fetch import checkout                # noqa: E402
from codeorbit import context as ctxmod         # noqa: E402
from codeorbit import query                     # noqa: E402

SPLITS = {
    "lite": "princeton-nlp/SWE-bench_Lite",
    "verified": "princeton-nlp/SWE-bench_Verified",
    "full": "princeton-nlp/SWE-bench",
    "multilingual": "SWE-bench/SWE-bench_Multilingual",     # 9 languages, no Python
}
CACHE = REPO / "bench" / "swebench"             # downloaded task files, not committed
CHECKOUTS = REPO / "bench" / "repos" / "swe"
QUESTION_CHARS = 4000       # issue text given to every arm; long issues are cut here



# --------------------------------------------------------------- dataset

def download(split: str) -> Path:
    """Fetch every task through the Hugging Face datasets-server, 100 at a time."""
    out = CACHE / f"{split}.jsonl"
    url = "https://datasets-server.huggingface.co/rows"
    rows, offset, total = [], 0, None
    while total is None or offset < total:
        r = requests.get(url, params={"dataset": SPLITS[split], "config": "default",
                                      "split": "test", "offset": offset, "length": 100},
                         timeout=60)
        r.raise_for_status()
        page = r.json()
        total = page["num_rows_total"]
        rows += [x["row"] for x in page["rows"]]
        offset += len(page["rows"])
        print(f"  {offset}/{total}", end="\r", flush=True)
        if not page["rows"]:
            break
    CACHE.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")
    print(f"wrote {len(rows)} tasks to {out}")
    return out


def load_tasks(split: str) -> list[dict]:
    path = CACHE / f"{split}.jsonl"
    if not path.exists():
        sys.exit(f"{path} not found - run: python scripts/swebench.py download --split {split}")
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def select(tasks: list[dict], repos: str | None, instances: str | None,
           limit: int | None, sample: int | None = None, seed: int = 0) -> list[dict]:
    if repos:
        keep = {r.strip() for r in repos.split(",")}
        tasks = [t for t in tasks if t["repo"] in keep]
    if instances:
        keep = {i.strip() for i in instances.split(",")}
        tasks = [t for t in tasks if t["instance_id"] in keep]
    if sample and sample < len(tasks):
        # A seeded random subset: same seed, same tasks, so a sampled result
        # can be rerun exactly. Random rather than first-N, which would all
        # come from one repository and one era of it.
        tasks = random.Random(seed).sample(tasks, sample)
    # Grouped by repository and then in commit date order, so each checkout
    # moves forward a little and the incremental index re-parses little.
    tasks = sorted(tasks, key=lambda t: (t["repo"], t["created_at"]))
    return tasks[:limit] if limit else tasks


# --------------------------------------------------------------- gold targets

_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+\d+(?:,\d+)? @@")


def patched_lines(patch: str) -> dict[str, set[int]]:
    """{old file path: line numbers in the ORIGINAL file the patch touches}.

    A removed line counts as itself. An added line has no original line, so it
    is attributed to the line just before it - the code it was added into.
    New files have no original and are skipped.
    """
    out: dict[str, set[int]] = {}
    path, line = None, 0
    for raw in patch.splitlines():
        if raw.startswith("--- "):
            src = raw[4:].strip()
            path = None if src == "/dev/null" else src.removeprefix("a/")
            continue
        if raw.startswith("+++ "):
            continue
        m = _HUNK.match(raw)
        if m:
            line = int(m.group(1))
            continue
        if path is None:
            continue
        tag = raw[0] if raw else " "    # a stripped blank context line
        if tag == " ":
            line += 1
        elif tag == "-":
            out.setdefault(path, set()).add(line)
            line += 1
        elif tag == "+":
            out.setdefault(path, set()).add(max(1, line - 1))
    return out


def gold_targets(conn, patch: str) -> tuple[list[str], list[str]]:
    """(function-level qnames, files) the fix touched, at the base commit."""
    qnames, files = [], []
    for path, lines in patched_lines(patch).items():
        if conn.execute("SELECT 1 FROM files WHERE path = ?", (path,)).fetchone() is None:
            continue                    # not a file CodeOrbit indexes (docs, config...)
        files.append(path)
        for ln in sorted(lines):
            # The outermost function/method containing the edit - a helper
            # nested inside a method rolls up to the method, as localisation
            # benchmarks count it. Class-level edits fall back to the class.
            row = conn.execute(
                "SELECT n.qname FROM nodes n JOIN files f ON f.id = n.file_id "
                "WHERE f.path = ? AND n.kind IN ('function', 'method') "
                "AND n.start_line <= ? AND n.end_line >= ? "
                "ORDER BY n.end_line - n.start_line DESC LIMIT 1",
                (path, ln, ln)).fetchone() or conn.execute(
                "SELECT n.qname FROM nodes n JOIN files f ON f.id = n.file_id "
                "WHERE f.path = ? AND n.kind = 'class' "
                "AND n.start_line <= ? AND n.end_line >= ? "
                "ORDER BY n.end_line - n.start_line LIMIT 1",
                (path, ln, ln)).fetchone()
            if row and row["qname"] not in qnames:
                qnames.append(row["qname"])
    return qnames, files


def question_kind(text: str, targets: list[str]) -> str:
    """named if the issue mentions a gold function by name, else unnamed."""
    for t in targets:
        if re.search(rf"\b{re.escape(t.rsplit('.', 1)[-1])}\b", text):
            return "named"
    return "unnamed"


# --------------------------------------------------------------- scoring

def first_rank(ranked, match) -> int | None:
    for i, u in enumerate(ranked[:bench.RANK_DEPTH], 1):
        if match(u):
            return i
    return None


def definition_lines(conn, src, targets: list[str]) -> list[str]:
    """Each target's definition line as written - the line naming it.

    What "its code is in the context" looks for in any language: the regex in
    bench.defines knows Python's def/class, not Go's func, Rust's fn or a Java
    method. Too-short lines are left out, so nothing matches by accident.
    """
    out = []
    for t in targets:
        row = conn.execute("SELECT n.start_line, n.end_line, f.path FROM nodes n "
                           "JOIN files f ON f.id = n.file_id WHERE n.qname = ? "
                           "ORDER BY n.end_line - n.start_line DESC LIMIT 1",
                           (t,)).fetchone()
        if row is None:
            continue
        lines = [ln.strip() for ln in src.body(row).splitlines()]
        tail = t.rsplit(".", 1)[-1]
        line = next((ln for ln in lines if re.search(rf"\b{re.escape(tail)}\b", ln)), "")
        if len(line) >= 8:
            out.append(line)
    return out


def score(o: bench.ArmOutput, targets: list[str], files: list[str],
          def_lines: list[str] = ()) -> dict:
    return {
        "rank": first_rank(o.ranked, lambda u: any(bench.unit_matches(u, t) for t in targets))
        if targets else None,
        "file_rank": first_rank(o.ranked, lambda u: u.path in files) if files else None,
        "hit_ctx": any(bench.contains_target(o.context, t) for t in targets),
        "def_ctx": any(bench.defines(o.context, t) for t in targets)
        or any(ln in o.context for ln in def_lines),
        "file_ctx": any(f in o.context for f in files),
    }


# --------------------------------------------------------------- tokens to locate
#
# How many tokens a reader spends before the code that needed fixing has passed
# in front of it:
#   without  a search's results, then the whole files they point to, in order -
#            what an agent does today (tok_files)
#   with     CodeOrbit's context, a few next function bodies, then file outlines
#            and the one function that matters (tok_symbols)
# Both are computed for every arm; the headline pairs grep files (without)
# against codeorbit (with). Not found within TOKEN_CAP: None.

TOKEN_CAP = 100_000


READ_BODIES = 5     # function bodies read past the context before switching to outlines


def tokens_symbols(o: bench.ArmOutput, targets: list[str], files: list[str], conn,
                   src, count, outlines: dict[str, int],
                   whole_file: dict[str, int] | None = None) -> int | None:
    """With CodeOrbit, used as its tools describe: the context; then the next few
    ranked function bodies; then each file's outline (codeorbit_outline) in the
    same order, and in a file that holds the fix, that function's body.

    That assumes the fix is recognised from its outline line, where the file
    reader sees real code - optimistic. With `whole_file` (path -> tokens) the
    right file, once reached, is read in full instead: a conservative bound."""
    spent = count(o.context)
    if any(bench.defines(o.context, t) for t in targets):
        return spent
    read = 0
    for u in o.ranked:
        if read == READ_BODIES:
            break
        body = _unread_body(o, u, conn, src)
        if body is None:
            continue
        read += 1
        spent += count(body)
        if any(bench.unit_matches(u, t) for t in targets):
            return spent

    seen = set()
    for u in o.ranked:
        if not u.path or u.path in seen:
            continue
        seen.add(u.path)
        if u.path not in outlines:
            path, rows = query.outline(conn, u.path)
            outlines[u.path] = count(ctxmod.format_outline(path, rows)) if path else 0
        spent += outlines[u.path]
        if spent > TOKEN_CAP:
            return None
        if u.path in files:
            if whole_file is not None:
                spent += whole_file[u.path]
                return spent if spent <= TOKEN_CAP else None
            body = _target_body(conn, src, targets, u.path)
            if body is not None:
                spent += count(body)
                return spent if spent <= TOKEN_CAP else None
    return None


def _target_body(conn, src, targets: list[str], path: str) -> str | None:
    for t in targets:
        row = conn.execute(
            "SELECT n.id, n.start_line, n.end_line, f.path AS path FROM nodes n "
            "JOIN files f ON f.id = n.file_id WHERE n.qname = ? AND f.path = ?",
            (t, path)).fetchone()
        if row:
            return src.body(row)
    return None


def _unread_body(o, u, conn, src) -> str | None:
    """A ranked function's body, or None if it is not a symbol or already shown."""
    if u.node_id is None:
        return None
    row = conn.execute(
        "SELECT n.id, n.start_line, n.end_line, f.path AS path FROM nodes n "
        "JOIN files f ON f.id = n.file_id WHERE n.id = ?", (u.node_id,)).fetchone()
    if row is None:
        return None
    body = src.body(row)
    first = body.strip().splitlines()[0] if body.strip() else ""
    if first and first in o.context:
        return None                     # already shown in the context
    return body


def tokens_files(o: bench.ArmOutput, files: list[str], root: Path, count,
                 cache: dict[str, int]) -> int | None:
    spent, seen = 0, set()
    for u in o.ranked:
        if not u.path or u.path in seen:
            continue
        seen.add(u.path)
        if u.path not in cache:
            try:
                cache[u.path] = count((root / u.path).read_text(
                    encoding="utf-8", errors="replace"))
            except OSError:
                cache[u.path] = 0
        spent += cache[u.path]
        if spent > TOKEN_CAP:
            return None
        if u.path in files:
            return spent
    return None


def token_summary(rows: list[dict]) -> dict:
    """Located-within-budget rates, and medians over tasks both readers located."""
    def within(key, cap):
        return sum(1 for r in rows if r.get(key) is not None and r[key] <= cap) / len(rows)

    both = [r for r in rows
            if r.get("tok_symbols") is not None and r.get("tok_files") is not None]
    out = {"n": len(rows)}
    for cap in (2_000, 8_000, 32_000):
        out[f"symbols_within_{cap // 1000}k"] = within("tok_symbols", cap)
        out[f"files_within_{cap // 1000}k"] = within("tok_files", cap)
    if both:
        sym = sorted(r["tok_symbols"] for r in both)
        fil = sorted(r["tok_files"] for r in both)
        ratio = sorted(r["tok_symbols"] / r["tok_files"] for r in both if r["tok_files"])
        out.update({"paired_n": len(both),
                    "median_symbols": sym[len(sym) // 2],
                    "median_files": fil[len(fil) // 2],
                    "median_saving": 1 - ratio[len(ratio) // 2] if ratio else None})
    return out


WITHOUT = "grep"        # how agents search today, then read the files it points to
WITH = "codeorbit"      # CodeOrbit's context, then its next functions


def with_without(rows: list[dict]) -> dict | None:
    """Tokens to locate the fix, with CodeOrbit vs without, task by task.

    'Without' is what a coding agent does by default: grep the issue's terms,
    then open the matching files in order of hits. 'With' is the product as
    shipped. Needs both arms in the run (grep is not a default arm).
    """
    a = {r["question_id"]: r for r in rows if r["arm"] == WITHOUT and r["fn_targets"]}
    b = {r["question_id"]: r for r in rows if r["arm"] == WITH and r["fn_targets"]}
    tasks = [t for t in a if t in b and "tok_files" in a[t]
             and "tok_symbols_conservative" in b[t]]
    if not tasks:
        return None
    series = {
        "without": [a[t]["tok_files"] for t in tasks],
        "with_conservative": [b[t]["tok_symbols_conservative"] for t in tasks],
        "with": [b[t]["tok_symbols"] for t in tasks],
    }

    def within(xs, cap):
        return sum(1 for x in xs if x is not None and x <= cap) / len(xs)

    def avg(xs):                # not found within the cap costs the cap
        return round(sum(x if x is not None else TOKEN_CAP for x in xs) / len(xs))

    def median(xs):
        xs = sorted(x if x is not None else TOKEN_CAP for x in xs)
        return xs[len(xs) // 2]

    out = {"n": len(tasks), "cap": TOKEN_CAP, "without_arm": WITHOUT, "with_arm": WITH}
    for key, xs in series.items():
        out[f"avg_{key}"] = avg(xs)
        out[f"median_{key}"] = median(xs)
        for cap in (2_000, 8_000, 32_000):
            out[f"{key}_within_{cap // 1000}k"] = within(xs, cap)
        if key != "without":
            out[f"fewer_tokens_{key}"] = sum(
                1 for w, c in zip(series["without"], xs)
                if (c if c is not None else TOKEN_CAP) < (w if w is not None else TOKEN_CAP))
    return out


def summarise(rows: list[dict], arms: list[str]) -> dict:
    out = {}
    for arm in arms:
        mine = [r for r in rows if r["arm"] == arm]
        fn = [r for r in mine if r["fn_targets"]]
        fl = [r for r in mine if r["files"]]
        m = {"function": bench.metrics(fn) if fn else {"n": 0}}
        if fn:
            m["function"]["ci95"] = {"hit_ctx": bench.bootstrap_ci(
                fn, lambda r: float(r["hit_ctx"]))}
            for kind in ("named", "unnamed"):
                sub = [r for r in fn if r["kind"] == kind]
                m[f"function_{kind}"] = bench.metrics(sub) if sub else {"n": 0}
        if fl:
            ranks = [r["file_rank"] for r in fl]
            m["file"] = {
                "n": len(fl),
                "recall@1": sum(1 for k in ranks if k and k <= 1) / len(fl),
                "recall@5": sum(1 for k in ranks if k and k <= 5) / len(fl),
                "recall@10": sum(1 for k in ranks if k) / len(fl),
                "file_ctx": sum(r["file_ctx"] for r in fl) / len(fl),
            }
        if fn and any("tok_symbols" in r for r in fn):
            m["tokens_to_locate"] = token_summary(fn)
        out[arm] = m
    ww = with_without(rows)
    if ww:
        out["with_vs_without_codeorbit"] = ww
    return out


# --------------------------------------------------------------- run

def run(args) -> None:
    from codeorbit import db
    from codeorbit.indexer import index_project
    from codeorbit.resolve import resolve_project

    all_tasks = load_tasks(args.split)
    held_out = 0
    if args.exclude_sample:
        # Drop the development sample: the same draw `--sample N --seed S` made
        # over the whole split, so the tasks looked at while tuning are never
        # scored again. Assumes that sample was drawn with no --repos filter.
        seen = {t["instance_id"] for t in
                select(all_tasks, None, None, None, args.exclude_sample, args.exclude_seed)}
        held_out = len(seen)
        all_tasks = [t for t in all_tasks if t["instance_id"] not in seen]
        print(f"excluding the {held_out} development tasks "
              f"(--sample {args.exclude_sample} --seed {args.exclude_seed}); "
              f"{len(all_tasks)} remain")
    tasks = select(all_tasks, args.repos, args.instances, args.limit,
                   args.sample, args.seed)
    if not tasks:
        sys.exit("no tasks selected")
    arms = [a.strip() for a in args.arms.split(",") if a.strip()]
    tok_name, count_tokens, tok_exact = bench.make_tokenizer(args.tokenizer)

    if args.embed:
        # Before any checkout or run folder: a dead Ollama otherwise surfaced
        # only after a clone and an index, or never (the arms were just dropped).
        from codeorbit import llm
        try:
            llm.embed(["probe"])
        except Exception as e:      # noqa: BLE001 - any failure means not usable
            sys.exit(f"--embed needs Ollama with {llm.EMBED_MODEL}, and embedding "
                     f"failed: {type(e).__name__}.\nStart it (ollama serve), pull the "
                     f"model (ollama pull {llm.EMBED_MODEL}), then run this again.")

    if args.resume:
        out = Path(args.resume)
        meta = json.loads((out / "meta.json").read_text(encoding="utf-8"))
        arms = meta["run"]["arms"]
        done = {json.loads(l)["question_id"] for l in
                (out / "per_question.jsonl").read_text(encoding="utf-8").splitlines() if l}
        print(f"resuming {out}: {len(done)} task(s) already done")
    else:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        out = Path(args.out) / f"{stamp}-swebench-{args.split}"
        out.mkdir(parents=True, exist_ok=True)
        done = set()
        # Written now, not at the end: a run that is interrupted must still say
        # what it was, and --resume needs it. `complete` flips at the end.
        meta = {
            "dataset": {"name": f"swebench-{args.split}", "source": SPLITS[args.split],
                        "sha256": hashlib.sha256(
                            (CACHE / f"{args.split}.jsonl").read_bytes()).hexdigest(),
                        "selection": {"repos": args.repos, "instances": args.instances,
                                      "limit": args.limit, "sample": args.sample,
                                      "seed": args.seed,
                                      "exclude_sample": args.exclude_sample,
                                      "exclude_seed": args.exclude_seed,
                                      "excluded": held_out}},
            "codeorbit": {"commit": bench._git("rev-parse", "HEAD", cwd=REPO),
                          "dirty": bool(bench._git("status", "--porcelain",
                                                   "--untracked-files=no", cwd=REPO))},
            "run": {"utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "arms": arms, "char_budget": args.budget,
                    "rank_depth": bench.RANK_DEPTH, "question_chars": QUESTION_CHARS,
                    "tokenizer": tok_name, "tokenizer_exact": tok_exact,
                    "embed": args.embed, "tasks_selected": len(tasks), "complete": False,
                    "fusion": dict(bench.FUSION)},
            "environment": {"python": sys.version.split()[0],
                            "platform": platform.platform(),
                            "packages": bench.package_versions()},
        }
        (out / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")

    log = (out / "per_question.jsonl").open("a", encoding="utf-8")
    live_seen = set()
    t_start = time.perf_counter()
    stopped_early = False
    warned = False

    for n, task in enumerate(tasks, 1):
        iid = task["instance_id"]
        if iid in done:
            continue
        if args.max_hours and time.perf_counter() - t_start > args.max_hours * 3600:
            # Stop between tasks, before a hosted session's hard limit kills the
            # process mid-write; --resume picks up from here.
            print(f"stopping after {args.max_hours} h (--max-hours); resume to continue")
            stopped_early = True
            break
        owner, name = task["repo"].split("/")
        dest = Path(args.checkouts) / f"{owner}__{name}"
        print(f"[{n}/{len(tasks)}] {iid}", flush=True)

        try:
            t0 = time.perf_counter()
            checkout(f"https://github.com/{task['repo']}", task["base_commit"], dest)
            index_project(dest)
            resolve_project(dest)
            index_s = time.perf_counter() - t0
            conn = db.connect(dest)
            if args.embed:
                from codeorbit import semantic
                semantic.build(conn, dest)
        except Exception as e:      # noqa: BLE001 - one bad task must not end the run
            print(f"    skipped: {type(e).__name__}: {e}")
            continue

        live, why = bench.semantic_probe(conn)
        live_seen.add(live)
        skipped = [a for a in arms if a in bench.SEMANTIC_ARMS] if not live else []
        if skipped and args.embed:
            # Asked for embeddings and they are not working: stop now. Carrying
            # on silently dropped dense/hybrid from every task of a run that
            # took hours, and nothing said so.
            conn.close()
            log.close()
            sys.exit(f"\nembeddings are not working: {why}\n"
                     f"--embed was given, so stopping rather than running without "
                     f"{', '.join(skipped)}.\nIs Ollama running? Check: "
                     f"curl http://127.0.0.1:11434/api/version")
        if skipped and not warned:
            print(f"    note: no embeddings ({why}) - skipping {', '.join(skipped)}")
            warned = True
        task_arms = [a for a in arms if a not in skipped]

        targets, files = gold_targets(conn, task["patch"])
        if not files:
            print("    skipped: the fix touches no indexed file")
            conn.close()
            continue

        text = task["problem_statement"][:QUESTION_CHARS]
        kind = question_kind(task["problem_statement"], targets)
        q = bench.Question(iid, text, targets[0] if targets else "", kind)
        src = bench.Sources(dest)
        needs_bm25 = {"bm25", "hybrid", "hybrid-graph", "fusion"}
        bm25 = bench.BM25(conn, src) if needs_bm25 & set(task_arms) else None

        def_lines = definition_lines(conn, src, targets)
        marks = []
        file_tokens: dict[str, int] = {}
        outline_tokens: dict[str, int] = {}
        for arm in task_arms:
            o = bench.run_arm(arm, q, dest, conn, src, bm25, args.budget, live)
            row = {
                "question_id": iid, "repo": task["repo"], "commit": task["base_commit"],
                "kind": kind, "arm": arm, "fn_targets": len(targets), "targets": targets,
                "files": files, **score(o, targets, files, def_lines),
                "chars": len(o.context), "tokens": count_tokens(o.context),
                "ms": round(1000 * o.seconds, 2), "index_s": round(index_s, 2),
                "top5": [u.label for u in o.ranked[:5]],
                **({"lists": o.lists} if o.lists else {}),
            }
            if targets:
                row["tok_symbols"] = tokens_symbols(o, targets, files, conn, src,
                                                    count_tokens, outline_tokens)
                row["tok_files"] = tokens_files(o, files, dest, count_tokens, file_tokens)
                for f in files:     # whole-file sizes for the conservative bound
                    if f not in file_tokens:
                        tokens_files(bench.ArmOutput([bench.Unit("", path=f)], "", 0),
                                     [f], dest, count_tokens, file_tokens)
                row["tok_symbols_conservative"] = tokens_symbols(
                    o, targets, files, conn, src, count_tokens, outline_tokens,
                    whole_file=file_tokens)
            log.write(json.dumps(row) + "\n")
            marks.append(f"{arm}={row['rank'] or '-'}/{row['file_rank'] or '-'}")
        log.flush()
        conn.close()
        print(f"    {len(targets)} fn target(s) in {len(files)} file(s), {kind}; "
              f"index {index_s:.1f}s\n    " + " ".join(marks), flush=True)

    log.close()
    rows = [json.loads(l) for l in
            (out / "per_question.jsonl").read_text(encoding="utf-8").splitlines() if l]
    if not rows:
        sys.exit("nothing was scored")
    arms_run = [a for a in arms if any(r["arm"] == a for r in rows)]

    # Live only if it was live for every task, across every resumed session.
    before = meta["run"].get("semantic_live")
    now = live_seen == {True} if live_seen else before
    meta["run"]["semantic_live"] = bool(now if before is None else before and now)
    meta["run"]["complete"] = not stopped_early
    meta["run"]["tasks_scored"] = len({r["question_id"] for r in rows})
    meta["run"]["wall_s"] = round(time.perf_counter() - t_start, 1)
    (out / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    summary = summarise(rows, arms_run)
    (out / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print_summary(summary, arms_run, rows)
    print(f"\nwrote {out}")


def print_summary(s: dict, arms: list[str], rows: list[dict]) -> None:
    tasks = {r["question_id"] for r in rows}
    with_fn = {r["question_id"] for r in rows if r["fn_targets"]}
    kinds = Counter(r["kind"] for r in rows if r["arm"] == arms[0] and r["fn_targets"])
    print(f"\n{len(tasks)} tasks ({len(with_fn)} with a function target: "
          f"{kinds['named']} named, {kinds['unnamed']} unnamed)")
    print("=" * 96)
    print(f"{'arm':<12}{'fn R@1':>8}{'R@5':>6}{'R@10':>6}{'MRR':>6}{'found':>7}{'code':>6}"
          f"{'file R@1':>10}{'R@5':>6}{'in ctx':>8}{'tokens':>8}{'ms p50':>8}")
    print("-" * 96)
    for arm in arms:
        f, fl = s[arm].get("function", {}), s[arm].get("file", {})
        if f.get("n"):
            fn = (f"{f['recall@1']:>8.0%}{f['recall@5']:>6.0%}{f['recall@10']:>6.0%}"
                  f"{f['mrr@10']:>6.2f}{f['hit_ctx']:>7.0%}{f['def_ctx']:>6.0%}")
        else:
            fn = f"{'-':>8}{'-':>6}{'-':>6}{'-':>6}{'-':>7}{'-':>6}"
        fi = (f"{fl['recall@1']:>10.0%}{fl['recall@5']:>6.0%}{fl['file_ctx']:>8.0%}"
              if fl else f"{'-':>10}{'-':>6}{'-':>8}")
        tok = f.get("avg_tokens", "-")
        ms = f.get("ms_p50", "-")
        print(f"{arm:<12}{fn}{fi}{tok:>8}{ms:>8}")

    w = s.get("with_vs_without_codeorbit")
    if not w:
        return
    print(f"\ntokens to locate the fix - with vs without CodeOrbit ({w['n']} tasks)")
    print("-" * 96)
    print(f"{'':<40}{'found within':^24}{'tokens per task':^20}{'saving':>8}")
    print(f"{'':<40}{'2k':>8}{'8k':>8}{'32k':>8}{'average':>10}{'median':>10}{'(avg)':>8}")
    for label, key in (("without (grep search + read files)", "without"),
                       ("with CodeOrbit - conservative", "with_conservative"),
                       ("with CodeOrbit - outline", "with")):
        saving = (f"{1 - w[f'avg_{key}'] / w['avg_without']:>8.0%}"
                  if key != "without" and w["avg_without"] else f"{'':>8}")
        print(f"{label:<40}" + "".join(f"{w[f'{key}_within_{k}k']:>8.0%}" for k in (2, 8, 32))
              + f"{w[f'avg_{key}']:>10,}{w[f'median_{key}']:>10,}{saving}")
    print(f"\nconservative: once the right file is reached it is read in full. outline: the"
          f" fix is\nrecognised from its outline line. The truth is between. Not found "
          f"within {w['cap']:,}\ntokens counts as {w['cap']:,}. CodeOrbit used fewer "
          f"tokens on {w['fewer_tokens_with_conservative']} (conservative) and "
          f"{w['fewer_tokens_with']} (outline) of {w['n']} tasks.")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("download", help="fetch a split's tasks into bench/swebench/")
    d.add_argument("--split", choices=SPLITS, default="lite")

    ls = sub.add_parser("list", help="tasks per repository in a downloaded split")
    ls.add_argument("--split", choices=SPLITS, default="lite")

    r = sub.add_parser("run", help="localise every selected task with every arm")
    r.add_argument("--split", choices=SPLITS, default="lite")
    r.add_argument("--repos", help="comma-separated, e.g. psf/requests,pallets/flask")
    r.add_argument("--instances", help="comma-separated instance ids")
    r.add_argument("--limit", type=int, help="first N tasks after filtering")
    r.add_argument("--sample", type=int, help="random N tasks after filtering")
    r.add_argument("--seed", type=int, default=0, help="seed for --sample")
    r.add_argument("--exclude-sample", type=int, metavar="N",
                   help="leave out the tasks --sample N --exclude-seed S would pick "
                        "(the development set), for a held-out final run")
    r.add_argument("--exclude-seed", type=int, default=0)
    r.add_argument("--arms", default=",".join(bench.DEFAULT_ARMS))
    r.add_argument("--budget", type=int, default=bench.CHAR_BUDGET)
    r.add_argument("--tokenizer", default="chars4",
                   help="chars4 | tiktoken:<encoding> | hf:<model-id>")
    r.add_argument("--embed", action="store_true",
                   help="build embeddings per checkout (slow on large repos; needs Ollama)")
    r.add_argument("--resume", help="continue an interrupted run directory")
    r.add_argument("--checkouts", default=str(CHECKOUTS),
                   help="where repositories are checked out - keep it outside a "
                        "synced folder (OneDrive, Google Drive): several GB of files")
    r.add_argument("--out", default=str(REPO / "bench" / "runs"),
                   help="where run folders are written, e.g. a mounted Drive on Colab")
    r.add_argument("--max-hours", type=float,
                   help="stop cleanly between tasks after this long (hosted sessions "
                        "with a hard limit, e.g. Kaggle's 12 h); resume to continue")
    args = ap.parse_args()

    if args.cmd == "download":
        download(args.split)
    elif args.cmd == "list":
        tasks = load_tasks(args.split)
        for repo, n in Counter(t["repo"] for t in tasks).most_common():
            print(f"{n:5}  {repo}")
        print(f"{len(tasks):5}  total")
    else:
        run(args)


if __name__ == "__main__":
    main()
