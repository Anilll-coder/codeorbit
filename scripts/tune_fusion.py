"""Tune fusion's name-search vote offline, from the lists a run saved.

A run with the fusion arm stores, per task, the three rankings it merged:
CodeOrbit's name search, BM25 and dense. Re-merging them under other settings
needs no repository, index or GPU, so every setting is scored in seconds.

Tune on the development tasks ONLY, then freeze the winner in
benchmark.FUSION and judge it on tasks it was not chosen on.

    python scripts/tune_fusion.py bench/runs/<dev run>
"""
from __future__ import annotations

import argparse
import itertools
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

import benchmark as bench                       # noqa: E402

WEIGHTS = [0.0, 0.25, 0.5, 0.75, 1.0]           # 0 = hybrid exactly
DEPTHS = [3, 5, 10, None]                        # None = the whole list
ONLY_CODED = [False, True]


def load(runs: list[Path]) -> list[dict]:
    """Tasks with saved lists and a function target, plus their issue text."""
    texts = {}
    cache = REPO / "bench" / "swebench" / "lite.jsonl"
    if cache.exists():
        for line in cache.read_text(encoding="utf-8").splitlines():
            t = json.loads(line)
            texts[t["instance_id"]] = t["problem_statement"]
    tasks = []
    for run in runs:
        for line in (run / "per_question.jsonl").read_text(encoding="utf-8").splitlines():
            r = json.loads(line)
            if r["arm"] == "fusion" and r.get("lists") and r["fn_targets"]:
                tasks.append({"id": r["question_id"], "targets": set(r["targets"]),
                              "lists": r["lists"], "text": texts.get(r["question_id"], "")})
    return tasks


def score(tasks: list[dict], settings: dict) -> dict:
    r1 = r5 = mrr = 0.0
    for t in tasks:
        L = t["lists"]
        ranked = bench.fuse(L["name"], L["bm25"], L["dense"], t["text"],
                            bench.RANK_DEPTH, settings)
        rank = next((i for i, q in enumerate(ranked, 1) if q in t["targets"]), None)
        r1 += rank == 1
        r5 += bool(rank and rank <= 5)
        mrr += 1 / rank if rank else 0
    n = len(tasks)
    return {"R@1": r1 / n, "R@5": r5 / n, "MRR": mrr / n}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("runs", nargs="+", help="run folders with the fusion arm")
    ap.add_argument("--top", type=int, default=12)
    args = ap.parse_args()

    tasks = load([Path(r) for r in args.runs])
    if not tasks:
        sys.exit("no fusion lists in these runs - they need the fusion arm, from a "
                 "version of swebench.py that saves them")
    if not any(t["text"] for t in tasks):
        print("note: bench/swebench/lite.jsonl not found - only_coded cannot be judged")

    hybrid = score(tasks, {"name_weight": 0.0, "name_depth": None, "only_coded": False})
    current = score(tasks, bench.FUSION)
    results = []
    for w, d, c in itertools.product(WEIGHTS[1:], DEPTHS, ONLY_CODED):
        s = {"name_weight": w, "name_depth": d, "only_coded": c}
        results.append((s, score(tasks, s)))

    # Ranked by MRR, the one score over all ranks; settings that also keep
    # hybrid's top-5 and fusion's top-1 are marked. Putting top-5 first chose
    # a setting that gave up top-1 and MRR on the development set.
    def key(item):
        return item[1]["MRR"]

    results.sort(key=key, reverse=True)
    print(f"{len(tasks)} tasks\n")
    print(f"{'setting':<44}{'R@1':>7}{'R@5':>7}{'MRR':>7}")
    print(f"{'hybrid (no name search)':<44}{hybrid['R@1']:>7.0%}{hybrid['R@5']:>7.0%}"
          f"{hybrid['MRR']:>7.3f}")
    print(f"{'fusion now ' + str(bench.FUSION):<44}"[:44] +
          f"{current['R@1']:>7.0%}{current['R@5']:>7.0%}{current['MRR']:>7.3f}")
    print("-" * 65)
    for s, m in results[:args.top]:
        mark = " <- keeps top-5 and top-1" if (m["R@5"] >= hybrid["R@5"]
                                               and m["R@1"] >= current["R@1"]) else ""
        label = f"w={s['name_weight']} depth={s['name_depth']} coded={s['only_coded']}"
        print(f"{label:<44}{m['R@1']:>7.0%}{m['R@5']:>7.0%}{m['MRR']:>7.3f}{mark}")
    best = results[0][0]
    if best == bench.FUSION or results[0][1] == current:
        print(f"\nbest by MRR: the current setting {bench.FUSION} - keep it")
    else:
        print(f"\nbest by MRR: {best}\nfreeze it in scripts/benchmark.py: FUSION = {best}")
    if not any(m["R@5"] >= hybrid["R@5"] and m["R@1"] >= current["R@1"]
               for _, m in results):
        print("no setting keeps both hybrid's top-5 and fusion's top-1 on these tasks")


if __name__ == "__main__":
    main()
