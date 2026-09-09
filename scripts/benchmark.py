"""A/B retrieval benchmark: CodeOrbit's graph vs a keyword baseline.

WHAT IS MEASURED, AND WHY THIS METRIC

The question is whether a graph finds the code that answers a question. That is
checkable without a model in the loop: each question has a ground-truth symbol
whose source actually contains the answer, and an arm either surfaced that
symbol or it did not. No LLM judging, no scoring rubric, no run-to-run variance.

Answer quality would need a judge, and a judge on a 3.8B model measures the
judge. Retrieval is the part this project actually builds; measure that.

THE BASELINE IS DELIBERATELY NOT A STRAWMAN

"Without CodeOrbit" is what a developer or a naive code-RAG does: grep the
question's identifiers across the repo, take the matching regions, stop at a
character budget. It gets the SAME budget as the graph arm, and it is given the
same keyword extraction the graph arm uses - so the only difference under test
is what each does after keyword matching, which is the honest comparison.

Two question classes, kept separate because they are not the same problem:
  named   - the question names a real identifier. grep should do well here.
  unnamed - the question names nothing that appears in the code. This is where
            a keyword baseline cannot work even in principle.
"""
from __future__ import annotations

import json
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from codeorbit import context as ctxmod          # noqa: E402
from codeorbit import db, query                  # noqa: E402
from codeorbit.scanner import scan               # noqa: E402

CHAR_BUDGET = 6000          # identical for both arms


@dataclass
class Question:
    text: str
    target: str             # qualified name of the symbol that answers it
    kind: str               # "named" or "unnamed"


# Ground truth was established by reading rich's source, not by running either
# arm - otherwise the benchmark would be scoring itself.
QUESTIONS = [
    # --- the question names an identifier that exists ------------------------
    Question("What does Console.print do to collect its renderables?",
             "console.Console._collect_renderables", "named"),
    Question("How does Segment.split_cells work?",
             "segment.Segment.split_cells", "named"),
    Question("What does Console.get_style do?",
             "console.Console.get_style", "named"),
    Question("How does render_lines produce output?",
             "console.Console.render_lines", "named"),
    Question("What does Text.append do?",
             "text.Text.append", "named"),
    Question("How does Table.add_row work?",
             "table.Table.add_row", "named"),
    Question("What does Style.combine do?",
             "style.Style.combine", "named"),
    Question("How does Color.parse handle its input?",
             "color.Color.parse", "named"),

    # --- the question names nothing that appears in the code -----------------
    Question("Where do we decide how wide the terminal is?",
             "console.Console.width", "unnamed"),
    Question("How does a piece of text get broken across several lines?",
             "text.Text.wrap", "unnamed"),
    Question("Where is the decision made about whether colour is supported?",
             "console.Console.color_system", "unnamed"),
    Question("How is a value turned into something printable?",
             "protocol.rich_cast", "unnamed"),
]


# --------------------------------------------------------------- baseline arm

def keyword_baseline(root: Path, question: str, budget: int = CHAR_BUDGET) -> str:
    """Grep the question's identifiers; return matching regions up to `budget`.

    Uses the same keyword extraction as the graph arm, so the comparison isolates
    what happens AFTER keyword matching rather than rewarding one arm for better
    tokenisation.
    """
    terms = ctxmod.keywords(question)
    if not terms:
        return ""

    patterns = [re.compile(re.escape(t), re.I) for t in terms]
    chunks: list[tuple[int, str]] = []

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
            chunks.append((hits, f"### {rel}:{lo + 1}\n{body}"))

    # Most keyword hits first - the ranking any grep-based approach would use.
    chunks.sort(key=lambda c: -c[0])
    out, size = [], 0
    for _score, text in chunks:
        if size + len(text) > budget:
            break
        out.append(text)
        size += len(text)
    return "\n\n".join(out)


# ------------------------------------------------------------ codeorbit arm

def graph_retrieval(root: Path, conn, question: str,
                    budget: int = CHAR_BUDGET) -> str:
    text, _picks = ctxmod.build(root, conn, question, max_symbols=4,
                                body_lines=60)
    return text[:budget]


# ------------------------------------------------------------------ scoring

def contains_target(text: str, target: str) -> bool:
    """Did the retrieved context actually include the answering symbol?

    Accepts the qualified name or the bare definition, because the baseline
    returns raw file regions that never carry a qualified name - requiring one
    would rig the result.
    """
    if not text:
        return False
    if target in text:
        return True
    tail = target.rsplit(".", 1)[-1]
    return bool(re.search(rf"(?:def|class)\s+{re.escape(tail)}\b", text))


@dataclass
class ArmResult:
    hits: int = 0
    total: int = 0
    chars: list[int] = field(default_factory=list)
    seconds: list[float] = field(default_factory=list)
    misses: list[str] = field(default_factory=list)

    @property
    def rate(self) -> float:
        return 100.0 * self.hits / self.total if self.total else 0.0

    @property
    def avg_chars(self) -> int:
        return int(sum(self.chars) / len(self.chars)) if self.chars else 0

    @property
    def avg_ms(self) -> int:
        return int(1000 * sum(self.seconds) / len(self.seconds)) if self.seconds else 0


def run(root: Path) -> dict:
    conn = db.connect(root)
    arms = {
        "baseline": {"named": ArmResult(), "unnamed": ArmResult()},
        "codeorbit": {"named": ArmResult(), "unnamed": ArmResult()},
    }

    for q in QUESTIONS:
        outcome = {}
        for arm, fn in (
            ("baseline", lambda: keyword_baseline(root, q.text)),
            ("codeorbit", lambda: graph_retrieval(root, conn, q.text)),
        ):
            t0 = time.perf_counter()
            text = fn()
            elapsed = time.perf_counter() - t0

            found = contains_target(text, q.target)
            outcome[arm] = found

            r = arms[arm][q.kind]
            r.total += 1
            r.chars.append(len(text))
            r.seconds.append(elapsed)
            if found:
                r.hits += 1
            else:
                r.misses.append(f"{q.text}  (wanted {q.target})")

        mark = lambda ok: "HIT " if ok else "miss"
        print(f"  {q.kind:8} {q.text[:52]:52} "
              f"baseline={mark(outcome['baseline'])}  "
              f"codeorbit={mark(outcome['codeorbit'])}", flush=True)

    conn.close()
    return arms


def main() -> None:
    root = Path(sys.argv[1] if len(sys.argv) > 1
                else ".venv/Lib/site-packages/rich").resolve()
    print(f"benchmark on {root}")
    print(f"{len(QUESTIONS)} questions, {CHAR_BUDGET} char budget per arm\n")

    arms = run(root)

    print("\n" + "=" * 74)
    header = f"{'arm':<11}{'class':<10}{'found the answer':<20}{'avg chars':<12}{'avg ms'}"
    print(header)
    print("-" * 74)
    for arm in ("baseline", "codeorbit"):
        for kind in ("named", "unnamed"):
            r = arms[arm][kind]
            print(f"{arm:<11}{kind:<10}"
                  f"{f'{r.hits}/{r.total}  ({r.rate:.0f}%)':<20}"
                  f"{r.avg_chars:<12,}{r.avg_ms}")
    print("-" * 74)
    for arm in ("baseline", "codeorbit"):
        h = sum(arms[arm][k].hits for k in ("named", "unnamed"))
        t = sum(arms[arm][k].total for k in ("named", "unnamed"))
        print(f"{arm:<11}{'OVERALL':<10}{f'{h}/{t}  ({100*h/t:.0f}%)':<20}")

    print("\nmisses:")
    for arm in ("baseline", "codeorbit"):
        for kind in ("named", "unnamed"):
            for m in arms[arm][kind].misses:
                print(f"  {arm:<10} {kind:<9} {m}")

    Path("benchmark-results.json").write_text(json.dumps({
        arm: {k: {"hits": r.hits, "total": r.total, "rate": round(r.rate, 1),
                  "avg_chars": r.avg_chars, "avg_ms": r.avg_ms,
                  "misses": r.misses}
              for k, r in kinds.items()}
        for arm, kinds in arms.items()
    }, indent=2), encoding="utf-8")
    print("\nwrote benchmark-results.json")


if __name__ == "__main__":
    main()
