"""Call-edge accuracy: sample call sites for hand labelling, then score them.

The README's "61% of call sites bound" is a binding rate - how many call sites
got an edge. It says nothing about whether those edges are right, and a wrong
edge misleads a model more than a missing one. This measures that.

  1. sample  - draw call sites at random (bound and unbound alike) into a CSV
               with what the resolver predicted for each.
  2. label   - a person fills the `gold` column by reading the code:
                 the correct target's qname, exactly as in `predicted`
                 -   the call leaves the project (len, a library method...)
                 ?   cannot tell; excluded from scoring
               Label without trusting `predicted` - it is shown so a correct
               one can be copied, not so it can be agreed with.
  3. score   - precision, recall and F1, overall and by resolution kind.

    python scripts/edge_eval.py sample .venv/Lib/site-packages/rich --n 150
    python scripts/edge_eval.py score bench/edges/rich.csv

Scoring, per call site with a gold label:
  TP  a predicted edge equals the gold target
  FP  each predicted edge that is not the gold target (incl. any edge when gold is -)
  FN  gold names an in-project target and no predicted edge equals it
"""
from __future__ import annotations

import argparse
import csv
import random
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

FIELDS = ["site", "file", "line", "caller", "call", "code", "predicted",
          "resolution", "gold", "notes"]


def sample(root: Path, n: int, seed: int, out: Path) -> None:
    from codeorbit import db
    if out.exists():
        sys.exit(f"{out} exists - refusing to overwrite labels")

    conn = db.connect(root)
    sites = conn.execute(
        "SELECT p.id, p.src, p.callee, p.recv, p.line, f.path, n.qname AS caller "
        "FROM pending_calls p JOIN files f ON f.id = p.file_id "
        "JOIN nodes n ON n.id = p.src ORDER BY p.id").fetchall()
    picked = sorted(random.Random(seed).sample(sites, min(n, len(sites))),
                    key=lambda s: (s["path"], s["line"]))

    lines: dict[str, list[str]] = {}
    rows = []
    for s in picked:
        # An edge does not record which call on its line it came from, so match
        # the target's name too - two calls on one line must not share edges.
        edges = conn.execute(
            "SELECT d.qname, e.resolution FROM edges e JOIN nodes d ON d.id = e.dst "
            "WHERE e.kind = 'calls' AND e.src = ? AND e.line = ? AND d.name = ? "
            "ORDER BY d.qname", (s["src"], s["line"], s["callee"])).fetchall()
        if s["path"] not in lines:
            lines[s["path"]] = (root / s["path"]).read_text(
                encoding="utf-8", errors="replace").splitlines()
        src_lines = lines[s["path"]]
        code = src_lines[s["line"] - 1].strip() if s["line"] <= len(src_lines) else ""
        rows.append({
            "site": s["id"], "file": s["path"], "line": s["line"], "caller": s["caller"],
            "call": f"{s['recv']}.{s['callee']}" if s["recv"] else s["callee"],
            "code": code[:160],
            "predicted": " | ".join(e["qname"] for e in edges),
            "resolution": ",".join(sorted({e["resolution"] for e in edges})) or "unbound",
            "gold": "", "notes": "",
        })
    conn.close()

    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)
    bound = sum(1 for r in rows if r["predicted"])
    print(f"wrote {len(rows)} call sites ({bound} bound) to {out}; fill the `gold` column")


def prf(tp: int, fp: int, fn: int) -> dict:
    p = tp / (tp + fp) if tp + fp else 0.0
    r = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * p * r / (p + r) if p + r else 0.0
    return {"tp": tp, "fp": fp, "fn": fn, "precision": p, "recall": r, "f1": f1}


def score_rows(rows: list[dict]) -> dict:
    groups: dict[str, list[int]] = {}
    for r in rows:
        gold = (r.get("gold") or "").strip()
        if gold in ("", "?"):
            continue
        pred = [p.strip() for p in (r.get("predicted") or "").split("|") if p.strip()]
        tp = int(gold != "-" and gold in pred)
        fp = sum(1 for p in pred if p != gold)
        fn = int(gold != "-" and gold not in pred)
        for key in ("overall", r.get("resolution") or "unbound"):
            g = groups.setdefault(key, [0, 0, 0, 0])
            g[0] += tp; g[1] += fp; g[2] += fn; g[3] += 1
    return {k: {**prf(*v[:3]), "sites": v[3]} for k, v in groups.items()}


def score(path: Path) -> None:
    with path.open(encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    labelled = sum(1 for r in rows if (r.get("gold") or "").strip() not in ("", "?"))
    print(f"{path}: {labelled} of {len(rows)} call sites labelled")
    if not labelled:
        return
    res = score_rows(rows)
    print(f"{'group':<12}{'sites':>6}{'TP':>5}{'FP':>5}{'FN':>5}"
          f"{'precision':>11}{'recall':>8}{'F1':>7}")
    for k in ["overall"] + sorted(k for k in res if k != "overall"):
        m = res[k]
        print(f"{k:<12}{m['sites']:>6}{m['tp']:>5}{m['fp']:>5}{m['fn']:>5}"
              f"{m['precision']:>11.1%}{m['recall']:>8.1%}{m['f1']:>7.1%}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sample", help="draw call sites into a CSV for labelling")
    s.add_argument("root")
    s.add_argument("--n", type=int, default=150)
    s.add_argument("--seed", type=int, default=0)
    s.add_argument("--out", help="default: bench/edges/<root name>.csv")
    c = sub.add_parser("score", help="precision/recall/F1 from a labelled CSV")
    c.add_argument("csv")
    args = ap.parse_args()

    if args.cmd == "sample":
        root = Path(args.root).resolve()
        out = Path(args.out) if args.out else REPO / "bench/edges" / f"{root.name}.csv"
        sample(root, args.n, args.seed, out)
    else:
        score(Path(args.csv))


if __name__ == "__main__":
    main()
