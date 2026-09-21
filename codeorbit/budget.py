"""Spend an agent's context deliberately instead of by accident.

The MCP tools exist to save an agent from reading files, and they only pay for
themselves if what they return is smaller than what the agent would otherwise
have read. That was not true of the first version. `codeorbit_explore` gave
every one of its four picks a flat 120-line body plus its full call
neighbourhood, and clipped whatever came out at 24,000 characters - roughly six
thousand tokens per call, most of it spent on the third and fourth pick, which
are the least likely to be what the question was about.

Three things were wasting most of it, and each has a fix here:

  * **A flat cap.** The first pick earns a long body; the fourth earns enough to
    recognise. `allocate` tapers, so a four-symbol answer costs about half of
    what four full bodies did while the symbol that matters is not shortened at
    all.

  * **The same code twice in one response.** Retrieval routinely returns a class
    and one of its own methods, and their bodies overlap exactly. `Ledger.claim`
    tracks the line ranges already emitted for a file so the second one is a
    pointer instead of a second copy.

  * **The same code in every response.** An agent's session is explore, then
    node, then impact, mostly over the same few symbols. The body does not
    change between those calls and the agent already has it. After the first
    two sends, the ledger replaces it with a citation and a note saying how to
    force it back - because the one thing worse than re-sending a body is
    withholding one the agent no longer has.

Clipping is still there as a backstop, but it should never fire: the point of
budgeting is to decide what to leave out *before* paying to format it, rather
than truncating a response that has already been built.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

# Tokens per character, for English prose mixed with code. Real tokenisers vary
# by model and this is not one; it is a budgeting ratio, and every number below
# is honest about being an estimate rather than a count.
CHARS_PER_TOKEN = 4


def estimate_tokens(text: str) -> int:
    """Rough token count. Used to report savings, never to enforce a limit."""
    return (len(text) + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN


def allocate(count: int, first: int, floor: int = 18) -> list[int]:
    """Line budgets for `count` symbols, ranked: most to the best match.

    Retrieval is ordered, and the ordering is the most reliable signal there
    is. Halving down the list and stopping at a floor keeps the top answer
    complete while the tail stays long enough to recognise - a signature, a
    docstring and the shape of the body.
    """
    out = []
    budget = first
    for _ in range(max(count, 0)):
        out.append(max(int(budget), floor))
        budget = budget * 0.55
    return out


@dataclass
class _Sent:
    times: int = 0
    ranges: list[tuple[int, int]] = field(default_factory=list)
    when: float = 0.0


# How many full sends a symbol gets before its body is replaced by a citation.
# Two, not one: an agent's context is compacted without warning, and a symbol
# asked about twice in a row is usually one it is actively working on.
FULL_SENDS = 2

# After this long, assume anything sent earlier has fallen out of the agent's
# context and start sending bodies again.
STALE_AFTER = 30 * 60      # seconds


class Ledger:
    """What has already been sent, per session and per response.

    Two scopes, because they answer different questions. `claim` is
    within-response: has this exact code already been printed *in the text I am
    building right now*. `should_send_body` is across-response: has this symbol
    been given to the agent recently enough that it still has it.
    """

    def __init__(self, full_sends: int = FULL_SENDS, stale_after: int = STALE_AFTER):
        self._sent: dict[int, _Sent] = {}
        self._full_sends = full_sends
        self._stale_after = stale_after
        self._response: set[tuple[str, int, int]] = set()

    # ------------------------------------------------- within one response

    def start_response(self) -> None:
        self._response = set()

    def claim(self, path: str, start: int, end: int) -> bool:
        """True if this span is new to the response; False if already covered.

        Covered, not merely equal: retrieval returning a class and one of its
        methods gives two spans where the smaller lies inside the larger, and
        printing the method's body again adds nothing.
        """
        for p, s, e in self._response:
            if p == path and s <= start and end <= e:
                return False
        self._response.add((path, start, end))
        return True

    # ----------------------------------------------------- across a session

    def should_send_body(self, node_id: int) -> bool:
        rec = self._sent.get(node_id)
        if rec is None:
            return True
        if time.time() - rec.when > self._stale_after:
            return True
        return rec.times < self._full_sends

    def record(self, node_id: int) -> None:
        rec = self._sent.setdefault(node_id, _Sent())
        if time.time() - rec.when > self._stale_after:
            rec.times = 0
        rec.times += 1
        rec.when = time.time()

    def forget(self, node_id: int | None = None) -> None:
        """Drop the memory of a symbol, or of everything. `detail="full"`
        goes through here, so asking again really does resend."""
        if node_id is None:
            self._sent.clear()
        else:
            self._sent.pop(node_id, None)


def trim_source(text: str) -> str:
    """Strip what costs tokens and carries nothing.

    Trailing whitespace is invisible and billed. A run of blank lines says the
    same thing as one blank line. Neither changes what the agent can do with
    the code, and on a real file the two together are a few per cent.
    """
    out: list[str] = []
    blanks = 0
    for line in text.split("\n"):
        line = line.rstrip()
        stripped = line.split("|", 1)[-1].strip() if "|" in line else line.strip()
        if not stripped:
            blanks += 1
            if blanks > 1:
                continue
        else:
            blanks = 0
        out.append(line)
    return "\n".join(out)
