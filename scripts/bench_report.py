"""Tabulate saved benchmark runs, per repository and pooled across them.

Reads only bench/runs/*/ - numbers in a write-up should come from here, never be
typed in. Pooled metrics are computed over all questions (micro average); the
per-repository rows show whether one repository is carrying the result.

    python scripts/bench_report.py                       # latest run per dataset
    python scripts/bench_report.py bench/runs/A bench/runs/B --markdown
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

from benchmark import ALL_ARMS, bootstrap_ci, metrics   # noqa: E402

COLS = [("mrr@10", "MRR"), ("recall@5", "R@5"), ("ndcg@10", "nDCG"),
        ("hit_ctx", "found"), ("def_ctx", "code"), ("avg_tokens", "tokens")]


def finished(run: Path) -> bool:
    """An interrupted run has no meta.json (older runs) or complete: false."""
    try:
        meta = json.loads((run / "meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return meta["run"].get("complete", True)


def latest_runs(runs_dir: Path) -> list[Path]:
    """The newest clean run of each dataset; dirty runs are not reportable."""
    best: dict[str, Path] = {}
    for d in sorted(runs_dir.glob("*/")):
        if not finished(d):
            continue
        meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
        if meta["codeorbit"].get("dirty"):
            continue
        best[meta["dataset"]["name"]] = d        # sorted by timestamp, so last wins
    return list(best.values())


def load(run: Path) -> tuple[dict, list[dict]]:
    meta = json.loads((run / "meta.json").read_text(encoding="utf-8"))
    rows = [json.loads(line) for line in
            (run / "per_question.jsonl").read_text(encoding="utf-8").splitlines() if line]
    # SWE-bench tasks whose fix is module-level have no function to rank;
    # they are file-level only (see scripts/swebench.py).
    rows = [r for r in rows if r.get("fn_targets", 1)]
    return meta, rows


def fmt(key: str, v) -> str:
    if key == "avg_tokens":
        return f"{v:,}"
    if key in ("hit_ctx", "def_ctx", "recall@5"):
        return f"{v:.0%}"
    return f"{v:.2f}"


def table(title: str, rows: list[dict], arms: list[str], markdown: bool) -> str:
    head = ["arm", *[c for _, c in COLS], "found 95% CI"]
    body = []
    for arm in arms:
        sub = [r for r in rows if r["arm"] == arm]
        if not sub:
            continue
        m = metrics(sub)
        ci = bootstrap_ci(sub, lambda r: float(r["hit_ctx"]))
        body.append([arm, *[fmt(k, m[k]) for k, _ in COLS],
                     f"{ci[0]:.0%}-{ci[1]:.0%}" if ci else ""])
    n = len({r["question_id"] for r in rows})
    if markdown:
        lines = [f"**{title}** ({n} questions)", "",
                 "| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
        lines += ["| " + " | ".join(r) + " |" for r in body]
    else:
        widths = [max(len(str(x)) for x in col) for col in zip(head, *body)]
        lines = [f"{title} ({n} questions)",
                 "  ".join(h.ljust(w) for h, w in zip(head, widths))]
        lines += ["  ".join(c.ljust(w) for c, w in zip(r, widths)) for r in body]
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("runs", nargs="*", help="run directories (default: latest per dataset)")
    ap.add_argument("--markdown", action="store_true")
    args = ap.parse_args()

    runs = [Path(r) for r in args.runs] or latest_runs(REPO / "bench/runs")
    for r in [r for r in runs if not finished(r)]:
        print(f"skipping {r}: interrupted run (resume it, or delete the folder)",
              file=sys.stderr)
    runs = [r for r in runs if finished(r)]
    if not runs:
        sys.exit("no clean runs found in bench/runs")

    pooled: list[dict] = []
    arms_seen: list[str] = []
    out = []
    for run in runs:
        meta, rows = load(run)
        arms = [a for a in ALL_ARMS if a in meta["run"]["arms"]]
        arms_seen += [a for a in arms if a not in arms_seen]
        flags = []
        if not meta["run"]["semantic_live"]:
            flags.append("embeddings OFF")
        if not meta["run"].get("tokenizer_exact"):
            flags.append("tokens estimated")
        title = (f"{meta['dataset']['name']} @ {run.name}"
                 + (f" [{', '.join(flags)}]" if flags else ""))
        out.append(table(title, rows, arms, args.markdown))
        pooled += [{**r, "question_id": f"{meta['dataset']['name']}/{r['question_id']}"}
                   for r in rows]

    if len(runs) > 1:
        # An arm missing from some runs would be pooled over fewer questions.
        common = [a for a in arms_seen
                  if all(a in load(r)[0]["run"]["arms"] for r in runs)]
        out.append(table("pooled", pooled, common, args.markdown))
    print("\n\n".join(out))


if __name__ == "__main__":
    main()
