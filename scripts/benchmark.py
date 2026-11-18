"""Retrieval benchmark: CodeOrbit's graph against standard code-retrieval baselines.

WHAT IS MEASURED, AND WHY THIS METRIC

The question is whether a retriever finds the code that answers a question. That
is checkable without a model in the loop: each question has a ground-truth symbol
whose source actually contains the answer, and an arm either surfaced that symbol
or it did not. No LLM judging, no scoring rubric, no run-to-run variance.

Answer quality would need a judge, and a judge on a 3.8B model measures the
judge. Retrieval is the part this project actually builds; measure that.

TWO FAMILIES OF METRIC, BECAUSE THEY ANSWER DIFFERENT QUESTIONS

  ranking  - Recall@1/5/10, MRR@10, nDCG@10 over each arm's ranked candidate
             list. One relevant item per question, so nDCG@10 is 1/log2(rank+1).
             This scores the ranking stage alone.
  context  - whether the text an arm would actually hand the model, cut to the
             SAME character budget for every arm, contains the answer:
               hit_ctx  the target's qualified name OR its definition appears
                        (the original pilot metric, kept for comparability)
               def_ctx  the target's definition itself appears - stricter,
                        because a callers list naming a symbol is not its code

THE ARMS

  grep         grep the question's identifiers, rank regions by hit count. The
               original pilot baseline; same keyword extraction as the graph.
  bm25         Okapi BM25 over function/class/method chunks, identifiers split
               on snake_case and camelCase. The standard lexical code-RAG.
  dense        embedding similarity only (needs Ollama + built embeddings).
  hybrid       bm25 + dense fused with the same RRF the graph arm uses.
  graph-kw     CodeOrbit with embeddings disabled (ablation: semantic layer).
  graph-noexp  full CodeOrbit retrieval, callers/callees/members removed from
               the context (ablation: graph expansion). Its ranking is
               identical to `codeorbit` by construction; only context differs.
  codeorbit    the product as shipped.

Every arm gets the same budget. Ranking metrics for the graph arms use
retrieve(max_symbols=10); the context uses the product's own 4-symbol build.

REPRODUCIBILITY

Each run writes bench/runs/<utc>-<dataset>/ containing meta.json (dataset, repo
pin and content hash, CodeOrbit commit, package versions, tokenizer, whether
embeddings were live), per_question.jsonl and .csv (one row per question per
arm, with the top-5 candidates), and summary.json. Plots and tables should be
generated from those files, never typed in.

    python scripts/benchmark.py                              # rich pilot set
    python scripts/benchmark.py --dataset bench/datasets/X.json --root path/to/X
    python scripts/benchmark.py --tokenizer tiktoken:o200k_base --measure-index
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import platform
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from codeorbit import context as ctxmod          # noqa: E402
from codeorbit import db, query                  # noqa: E402
from codeorbit.scanner import scan, sha1         # noqa: E402

CHAR_BUDGET = 6000          # identical for every arm
RANK_DEPTH = 10             # candidates scored by the ranking metrics
ALL_ARMS = ["grep", "bm25", "dense", "hybrid", "graph-kw", "graph-noexp", "codeorbit"]
SEMANTIC_ARMS = {"dense", "hybrid"}


@dataclass
class Question:
    id: str
    text: str
    target: str             # qualified name of the symbol that answers it
    kind: str               # "named" or "unnamed"
    verified: bool = True   # target checked against the source by a person


@dataclass
class Unit:
    """One ranked candidate. Graph-backed units carry a qname; grep regions don't."""
    text: str
    qname: str | None = None
    label: str = ""
    path: str = ""          # repo-relative file, for file-level scoring


def load_dataset(path: Path) -> tuple[dict, list[Question]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    qs = [Question(q["id"], q["text"], q["target"], q["kind"], q.get("verified", True))
          for q in data["questions"]]
    ids = [q.id for q in qs]
    if len(ids) != len(set(ids)):
        sys.exit(f"{path}: duplicate question ids")
    return data, qs


# --------------------------------------------------------------- matching

def defines(text: str, target: str) -> bool:
    tail = target.rsplit(".", 1)[-1]
    return bool(re.search(rf"(?:def|class)\s+{re.escape(tail)}\b", text))


def contains_target(text: str, target: str) -> bool:
    """Did the retrieved context include the answering symbol?

    Accepts the qualified name or the bare definition, because grep returns raw
    file regions that never carry a qualified name - requiring one would rig
    the result.
    """
    return bool(text) and (target in text or defines(text, target))


def unit_matches(u: Unit, target: str) -> bool:
    return u.qname == target if u.qname is not None else defines(u.text, target)


# --------------------------------------------------------------- source access

class Sources:
    """File lines, read once. Symbol bodies are sliced from here."""

    def __init__(self, root: Path):
        self.root = root
        self._lines: dict[str, list[str]] = {}

    def body(self, row) -> str:
        lines = self._lines.get(row["path"])
        if lines is None:
            try:
                lines = (self.root / row["path"]).read_text(
                    encoding="utf-8", errors="replace").splitlines()
            except OSError:
                lines = []
            self._lines[row["path"]] = lines
        return "\n".join(lines[row["start_line"] - 1:row["end_line"]])


def symbol_unit(src: Sources, row) -> Unit:
    head = (f"### {row['kind']} {row['qname']}\n"
            f"defined at {row['path']}:{row['start_line']}-{row['end_line']}")
    return Unit(f"{head}\n```{row['lang']}\n{src.body(row)}\n```",
                qname=row["qname"], label=row["qname"], path=row["path"])


def pack(units: list[Unit], budget: int) -> str:
    """Concatenate units best-first until the budget would be exceeded.

    The first unit is truncated rather than dropped if it alone is too large,
    so an arm whose best hit is a big class is not handed an empty context.
    """
    out, size = [], 0
    for u in units:
        if size + len(u.text) > budget:
            if not out:
                out.append(u.text[:budget])
            break
        out.append(u.text)
        size += len(u.text) + 2
    return "\n\n".join(out)


# --------------------------------------------------------------- grep arm

def grep_units(root: Path, question: str) -> list[Unit]:
    """Grep the question's identifiers; matching regions, most hits first.

    Uses the same keyword extraction as the graph arm, so the comparison isolates
    what happens AFTER keyword matching rather than rewarding one arm for better
    tokenisation.
    """
    terms = ctxmod.keywords(question)
    if not terms:
        return []

    patterns = [re.compile(re.escape(t), re.I) for t in terms]
    chunks: list[tuple[int, Unit]] = []

    for path, _lang in scan(root):
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        rel = path.relative_to(root).as_posix()
        for i, line in enumerate(lines):
            hits = sum(1 for p in patterns if p.search(line))
            if not hits:
                continue
            # A grep-style window around the hit, as a person would read.
            lo, hi = max(0, i - 8), min(len(lines), i + 25)
            body = "\n".join(lines[lo:hi])
            chunks.append((hits, Unit(f"### {rel}:{lo + 1}\n{body}",
                                      label=f"{rel}:{lo + 1}", path=rel)))

    # Most keyword hits first - the ranking any grep-based approach would use.
    chunks.sort(key=lambda c: -c[0])
    return [u for _, u in chunks]


# --------------------------------------------------------------- bm25 arm

_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_CAMEL = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|[0-9]+")


def code_tokens(text: str) -> list[str]:
    """Identifiers, plus their snake_case / camelCase parts, lower-cased."""
    out = []
    for ident in _IDENT.findall(text):
        low = ident.lower()
        out.append(low)
        parts = [p.lower() for piece in ident.split("_") for p in _CAMEL.findall(piece)]
        if len(parts) > 1:
            out.extend(p for p in parts if len(p) > 1)
    return out


class BM25:
    """Okapi BM25 over function/class/method chunks.

    Module nodes are left out: a whole file is a chunk no code-RAG would use,
    and it would win every query on term frequency alone.
    """

    def __init__(self, conn, src: Sources, k1: float = 1.2, b: float = 0.75):
        self.k1, self.b = k1, b
        self.ids: list[int] = []
        self.lengths: list[int] = []
        self.postings: dict[str, list[tuple[int, int]]] = {}
        rows = conn.execute(
            f"SELECT {query.NODE_COLS} FROM nodes n JOIN files f ON f.id = n.file_id "
            "WHERE n.kind != 'module'").fetchall()
        for row in rows:
            doc = " ".join(filter(None, [
                row["kind"], row["name"], row["qname"], row["signature"],
                row["docstring"], src.body(row)]))
            toks = code_tokens(doc)
            tf: dict[str, int] = {}
            for t in toks:
                tf[t] = tf.get(t, 0) + 1
            d = len(self.ids)
            self.ids.append(row["id"])
            self.lengths.append(len(toks))
            for t, n in tf.items():
                self.postings.setdefault(t, []).append((d, n))
        self.n = len(self.ids)
        self.avgdl = (sum(self.lengths) / self.n) if self.n else 1.0

    def search(self, question: str, limit: int) -> list[int]:
        terms = {t for t in code_tokens(question) if t not in ctxmod.STOP}
        scores: dict[int, float] = {}
        for t in terms:
            post = self.postings.get(t)
            if not post:
                continue
            idf = math.log(1 + (self.n - len(post) + 0.5) / (len(post) + 0.5))
            for d, tf in post:
                norm = tf + self.k1 * (1 - self.b + self.b * self.lengths[d] / self.avgdl)
                scores[d] = scores.get(d, 0.0) + idf * tf * (self.k1 + 1) / norm
        best = sorted(scores.items(), key=lambda p: -p[1])[:limit]
        return [self.ids[d] for d, _ in best]


# --------------------------------------------------------------- fusion

def rrf(lists: list[list[int]], limit: int) -> list[int]:
    fused: dict[int, float] = {}
    for ids in lists:
        for rank, nid in enumerate(ids):
            fused[nid] = fused.get(nid, 0.0) + 1.0 / (ctxmod.RRF_K + rank + 1)
    return [nid for nid, _ in sorted(fused.items(), key=lambda p: -p[1])[:limit]]


def node_units(conn, src: Sources, ids: list[int]) -> list[Unit]:
    out = []
    for nid in ids:
        row = query.get_node(conn, nid)
        if row is not None:
            out.append(symbol_unit(src, row))
    return out


def dense_ids(conn, question: str, limit: int) -> list[int]:
    from codeorbit import semantic as sem
    return [nid for _, nid in sem.search(conn, question, limit=limit)]


# --------------------------------------------------------------- arms

@dataclass
class ArmOutput:
    ranked: list[Unit]
    context: str
    seconds: float          # time to produce the context a model would see


def run_arm(arm: str, q: Question, root: Path, conn, src: Sources,
            bm25: BM25 | None, budget: int, semantic_live: bool) -> ArmOutput:
    pool = 50               # candidates fetched before packing / fusion

    if arm in ("graph-kw", "graph-noexp", "codeorbit"):
        # Off when embeddings are not live, so latency is not padded by the
        # embedding call timing out on every question.
        semantic = semantic_live and arm != "graph-kw"
        t0 = time.perf_counter()
        text, _picks = ctxmod.build(root, conn, q.text, max_symbols=4, body_lines=60,
                                    semantic=semantic,
                                    neighbours=arm != "graph-noexp")
        secs = time.perf_counter() - t0
        rows = ctxmod.retrieve(conn, q.text, RANK_DEPTH, semantic=semantic)
        ranked = [Unit("", qname=r["qname"], label=r["qname"], path=r["path"])
                  for r in rows]
        return ArmOutput(ranked, text[:budget], secs)

    t0 = time.perf_counter()
    if arm == "grep":
        units = grep_units(root, q.text)
    elif arm == "bm25":
        units = node_units(conn, src, bm25.search(q.text, pool))
    elif arm == "dense":
        units = node_units(conn, src, dense_ids(conn, q.text, pool))
    elif arm == "hybrid":
        units = node_units(conn, src, rrf(
            [bm25.search(q.text, pool), dense_ids(conn, q.text, pool)], pool))
    else:
        raise ValueError(arm)
    text = pack(units, budget)
    return ArmOutput(units[:RANK_DEPTH], text, time.perf_counter() - t0)


# --------------------------------------------------------------- tokenizers

def make_tokenizer(spec: str):
    """(name, count_fn, exact). Real tokenizers are optional dependencies."""
    if spec == "chars4":
        return "chars/4 (estimate)", lambda s: round(len(s) / 4), False
    kind, _, name = spec.partition(":")
    if kind == "tiktoken":
        import tiktoken
        enc = tiktoken.get_encoding(name)
        return f"tiktoken {name} {tiktoken.__version__}", \
            lambda s: len(enc.encode(s, disallowed_special=())), True
    if kind == "hf":
        import tokenizers
        tok = tokenizers.Tokenizer.from_pretrained(name)
        return f"hf {name} (tokenizers {tokenizers.__version__})", \
            lambda s: len(tok.encode(s, add_special_tokens=False).ids), True
    sys.exit(f"unknown --tokenizer {spec!r}; use chars4, tiktoken:<enc> or hf:<model>")


# --------------------------------------------------------------- environment

def _git(*args: str, cwd: Path) -> str | None:
    try:
        return subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                              text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def package_versions() -> dict:
    from importlib import metadata
    out = {}
    for dist in ("codeorbit", "tree-sitter", "tree-sitter-language-pack",
                 "tree-sitter-python", "tree-sitter-javascript",
                 "tree-sitter-typescript", "tiktoken", "tokenizers"):
        try:
            out[dist] = metadata.version(dist)
        except metadata.PackageNotFoundError:
            pass
    return out


def repo_identity(root: Path, pin: dict) -> dict:
    """What was actually benchmarked, and whether it matches the dataset's pin."""
    files = sorted((p.relative_to(root).as_posix(), sha1(p.read_bytes())) for p, _ in scan(root))
    h = hashlib.sha256("\n".join(f"{r} {s}" for r, s in files).encode()).hexdigest()
    ident: dict = {"path": str(root), "files": len(files), "content_sha256": h,
                   "pin": pin, "pin_ok": None}

    if "commit" in pin:
        head = _git("rev-parse", "HEAD", cwd=root)
        ident["commit"] = head
        ident["pin_ok"] = head is not None and head.startswith(pin["commit"])
    elif "pypi" in pin:
        from importlib import metadata
        try:
            ver = metadata.version(pin["pypi"])
        except metadata.PackageNotFoundError:
            ver = None
        ident["installed_version"] = ver
        ident["pin_ok"] = ver == pin.get("version")
    return ident


def semantic_probe(conn) -> tuple[bool, str]:
    """Embeddings are only live if they exist AND a query can be embedded now."""
    from codeorbit import llm, semantic as sem
    if not sem.available(conn):
        return False, "no embeddings in the index (run `codeorbit embed`)"
    try:
        llm.embed(["probe"])
    except Exception as e:      # noqa: BLE001 - any failure means not live
        return False, f"embedding model unreachable: {type(e).__name__}"
    return True, f"{sem.count(conn)} symbols embedded with {llm.EMBED_MODEL}"


_PEAK_RSS = r'''
import json, sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[2])
from codeorbit.indexer import index_project
from codeorbit.resolve import resolve_project
root = Path(sys.argv[1])
t0 = time.perf_counter(); index_project(root, full=True); t1 = time.perf_counter()
st = resolve_project(root); t2 = time.perf_counter()
if sys.platform == "win32":
    import ctypes, ctypes.wintypes as w
    class PMC(ctypes.Structure):
        _fields_ = [("cb", w.DWORD), ("PageFaultCount", w.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                    ("a", ctypes.c_size_t), ("b", ctypes.c_size_t), ("c", ctypes.c_size_t),
                    ("d", ctypes.c_size_t), ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t)]
    pmc = PMC(); pmc.cb = ctypes.sizeof(PMC)
    k = ctypes.windll.kernel32; k.GetCurrentProcess.restype = w.HANDLE
    gpmi = ctypes.windll.psapi.GetProcessMemoryInfo
    gpmi.argtypes = [w.HANDLE, ctypes.POINTER(PMC), w.DWORD]
    gpmi(k.GetCurrentProcess(), ctypes.byref(pmc), pmc.cb)
    peak = pmc.PeakWorkingSetSize
else:
    import resource
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    peak *= 1 if sys.platform == "darwin" else 1024
print(json.dumps({"parse_s": t1 - t0, "resolve_s": t2 - t1, "peak_rss_bytes": peak,
                  "stats": {k: v for k, v in st.items() if isinstance(v, (int, float))}}))
'''


def measure_index(root: Path) -> dict:
    """Index a scratch copy in a fresh process, so the live index is untouched
    and peak RSS is the indexer's alone."""
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / root.name
        shutil.copytree(root, copy, ignore=shutil.ignore_patterns(".codeorbit", "__pycache__"))
        proc = subprocess.run([sys.executable, "-c", _PEAK_RSS, str(copy), str(REPO)],
                              capture_output=True, text=True)
        if proc.returncode:
            return {"error": proc.stderr.strip().splitlines()[-1:]}
        out = json.loads(proc.stdout.strip().splitlines()[-1])
        out["db_bytes"] = db.db_path(copy).stat().st_size
        return out


# --------------------------------------------------------------- metrics

def first_rank(ranked: list[Unit], target: str) -> int | None:
    for i, u in enumerate(ranked[:RANK_DEPTH], 1):
        if unit_matches(u, target):
            return i
    return None


def metrics(rows: list[dict]) -> dict:
    n = len(rows)
    if not n:
        return {"n": 0}
    ranks = [r["rank"] for r in rows]
    ms = sorted(r["ms"] for r in rows)

    def mean(xs):
        return sum(xs) / len(xs)

    return {
        "n": n,
        "recall@1": mean([1.0 if k and k <= 1 else 0.0 for k in ranks]),
        "recall@5": mean([1.0 if k and k <= 5 else 0.0 for k in ranks]),
        "recall@10": mean([1.0 if k else 0.0 for k in ranks]),
        "mrr@10": mean([1.0 / k if k else 0.0 for k in ranks]),
        "ndcg@10": mean([1.0 / math.log2(k + 1) if k else 0.0 for k in ranks]),
        "hit_ctx": mean([float(r["hit_ctx"]) for r in rows]),
        "def_ctx": mean([float(r["def_ctx"]) for r in rows]),
        "avg_chars": round(mean([r["chars"] for r in rows])),
        "avg_tokens": round(mean([r["tokens"] for r in rows])),
        "ms_mean": round(mean(ms), 1),
        "ms_p50": round(ms[len(ms) // 2], 1),
        "ms_p95": round(ms[min(len(ms) - 1, math.ceil(0.95 * len(ms)) - 1)], 1),
    }


def bootstrap_ci(rows: list[dict], key, iters: int = 2000, seed: int = 0):
    """95% percentile interval over questions. Wide on small sets - that is the point."""
    if len(rows) < 2:
        return None
    rng = random.Random(seed)
    vals = [key(r) for r in rows]
    means = sorted(sum(rng.choice(vals) for _ in vals) / len(vals) for _ in range(iters))
    return [round(means[int(0.025 * iters)], 4), round(means[int(0.975 * iters) - 1], 4)]


def summarise(per_q: list[dict], arms: list[str]) -> dict:
    out = {}
    for arm in arms:
        rows = [r for r in per_q if r["arm"] == arm]
        block = {"overall": metrics(rows)}
        for kind in sorted({r["kind"] for r in rows}):
            block[kind] = metrics([r for r in rows if r["kind"] == kind])
        block["overall"]["ci95"] = {
            "hit_ctx": bootstrap_ci(rows, lambda r: float(r["hit_ctx"])),
            "mrr@10": bootstrap_ci(rows, lambda r: 1.0 / r["rank"] if r["rank"] else 0.0),
        }
        out[arm] = block
    return out


# --------------------------------------------------------------- main

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("root", nargs="?", help="repository to benchmark "
                    "(default: the dataset's default_path)")
    ap.add_argument("--dataset", default=str(REPO / "bench/datasets/rich.json"))
    ap.add_argument("--arms", default=",".join(ALL_ARMS),
                    help=f"comma-separated subset of {','.join(ALL_ARMS)}")
    ap.add_argument("--budget", type=int, default=CHAR_BUDGET)
    ap.add_argument("--tokenizer", default="chars4",
                    help="chars4 | tiktoken:<encoding> | hf:<model-id>")
    ap.add_argument("--out", default=str(REPO / "bench/runs"))
    ap.add_argument("--include-unverified", action="store_true",
                    help="also score questions whose target nobody has checked yet")
    ap.add_argument("--measure-index", action="store_true",
                    help="also time a full index of a scratch copy and record peak RSS")
    args = ap.parse_args()

    data, questions = load_dataset(Path(args.dataset))
    unverified = [q for q in questions if not q.verified]
    if unverified and not args.include_unverified:
        # Drafted targets are guesses until someone reads the source; scoring
        # them would let the benchmark grade itself.
        questions = [q for q in questions if q.verified]
        print(f"skipping {len(unverified)} unverified question(s); "
              f"--include-unverified to score them anyway")
    if not questions:
        sys.exit("no questions to run")
    root = (Path(args.root) if args.root else REPO / data["repo"]["default_path"]).resolve()
    arms = [a.strip() for a in args.arms.split(",") if a.strip()]
    for a in arms:
        if a not in ALL_ARMS:
            sys.exit(f"unknown arm {a!r}")
    tok_name, count_tokens, tok_exact = make_tokenizer(args.tokenizer)

    if not db.db_path(root).exists():
        sys.exit(f"{root} has no index - run `codeorbit index {root}` first")
    conn = db.connect(root)

    live, why = semantic_probe(conn)
    if not live:
        dropped = [a for a in arms if a in SEMANTIC_ARMS]
        arms = [a for a in arms if a not in SEMANTIC_ARMS]
        print(f"!! embeddings not live: {why}")
        if dropped:
            print(f"!! skipping {', '.join(dropped)}")
        print("!! `codeorbit` and `graph-noexp` will run keyword-only, "
              "i.e. identical retrieval to `graph-kw`\n")

    print(f"dataset {data['name']}: {len(questions)} questions on {root}")
    print(f"arms {', '.join(arms)}; {args.budget} char budget; tokens by {tok_name}\n")

    print("hashing repository...", flush=True)
    ident = repo_identity(root, data["repo"].get("pin", {}))
    if ident["pin_ok"] is False:
        print(f"!! repository does not match the dataset pin {ident['pin']}")

    src = Sources(root)
    bm25, bm25_secs = None, None
    if {"bm25", "hybrid"} & set(arms):
        t0 = time.perf_counter()
        bm25 = BM25(conn, src)
        bm25_secs = time.perf_counter() - t0

    per_q: list[dict] = []
    for q in questions:
        marks = []
        for arm in arms:
            o = run_arm(arm, q, root, conn, src, bm25, args.budget, live)
            rank = first_rank(o.ranked, q.target)
            row = {
                "question_id": q.id, "kind": q.kind, "arm": arm, "target": q.target,
                "rank": rank,
                "hit_ctx": contains_target(o.context, q.target),
                "def_ctx": defines(o.context, q.target),
                "chars": len(o.context), "tokens": count_tokens(o.context),
                "ms": round(1000 * o.seconds, 2),
                "top5": [u.label for u in o.ranked[:5]],
            }
            per_q.append(row)
            marks.append(f"{arm}={rank or '-'}{'*' if row['hit_ctx'] else ''}")
        print(f"  {q.id:9} {q.text[:44]:44} {' '.join(marks)}", flush=True)
    print("  (rank of target in top-10, '-' if absent; '*' = in budgeted context)")

    conn.close()

    index_cost = measure_index(root) if args.measure_index else None
    summary = summarise(per_q, arms)

    meta = {
        "dataset": {"path": Path(args.dataset).resolve().relative_to(REPO).as_posix()
                    if Path(args.dataset).resolve().is_relative_to(REPO) else args.dataset,
                    "name": data["name"], "questions": len(questions),
                    "unverified_included": sum(not q.verified for q in questions),
                    "sha256": hashlib.sha256(Path(args.dataset).read_bytes()).hexdigest()},
        "repository": ident,
        "codeorbit": {"commit": _git("rev-parse", "HEAD", cwd=REPO),
                      # Untracked files are notes and scratch work, not the
                      # code that ran; only tracked changes make a run dirty.
                      "dirty": bool(_git("status", "--porcelain",
                                         "--untracked-files=no", cwd=REPO))},
        "run": {"utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "arms": arms, "char_budget": args.budget, "rank_depth": RANK_DEPTH,
                "rrf_k": ctxmod.RRF_K, "semantic_live": live, "semantic_detail": why,
                "tokenizer": tok_name, "tokenizer_exact": tok_exact,
                "bm25_index_s": round(bm25_secs, 3) if bm25_secs is not None else None},
        "environment": {"python": sys.version.split()[0], "platform": platform.platform(),
                        "machine": platform.machine(), "packages": package_versions()},
        "index_cost": index_cost,
    }

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = Path(args.out) / f"{stamp}-{data['name']}"
    out.mkdir(parents=True, exist_ok=True)
    (out / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    (out / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    with (out / "per_question.jsonl").open("w", encoding="utf-8") as f:
        for r in per_q:
            f.write(json.dumps(r) + "\n")
    with (out / "per_question.csv").open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(per_q[0].keys()))
        w.writeheader()
        for r in per_q:
            w.writerow({**r, "top5": " | ".join(r["top5"])})

    print("\n" + "=" * 92)
    print(f"{'arm':<12}{'R@1':>6}{'R@5':>6}{'R@10':>6}{'MRR':>6}{'nDCG':>6}"
          f"{'hit_ctx':>9}{'def_ctx':>9}{'tokens':>8}{'ms p50':>8}   hit_ctx 95% CI")
    print("-" * 92)
    for arm in arms:
        m = summary[arm]["overall"]
        ci = m["ci95"]["hit_ctx"]
        print(f"{arm:<12}{m['recall@1']:>6.2f}{m['recall@5']:>6.2f}{m['recall@10']:>6.2f}"
              f"{m['mrr@10']:>6.2f}{m['ndcg@10']:>6.2f}{m['hit_ctx']:>9.0%}{m['def_ctx']:>9.0%}"
              f"{m['avg_tokens']:>8}{m['ms_p50']:>8}   "
              f"{f'[{ci[0]:.0%}, {ci[1]:.0%}]' if ci else ''}")
    print("-" * 92)
    for kind in sorted({q.kind for q in questions}):
        print(f"{kind}: " + "  ".join(
            f"{arm} {summary[arm][kind]['hit_ctx']:.0%}" for arm in arms))
    if index_cost:
        print(f"\nindex: {index_cost}")
    print(f"\nwrote {out}")


if __name__ == "__main__":
    main()
