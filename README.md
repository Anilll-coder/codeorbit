# CodeOrbit

Local-first code intelligence. It parses a repository with tree-sitter, builds a
structural knowledge graph of its symbols and their relationships in SQLite, and
answers questions about the code using a **local** LLM through Ollama.

Nothing leaves the machine. No API keys, no cloud, no telemetry.

## The idea

A small local model (3.8B, CPU-only) cannot reason its way out of bad context.
So the graph does the work, not the model.

Naive RAG over code retrieves *chunks that look like the question*. This
retrieves **the subgraph the question lives in** — the symbol, its source, who
calls it, and what it calls. That neighbourhood is what a human reader would
need, and it is exactly what plain text search cannot give you.

The claim this project tests: **graph-grounded retrieval lets a 3.8B local model
answer questions that need whole-codebase context.**

## Install

**Linux / macOS / Git Bash**

```sh
curl -fsSL https://raw.githubusercontent.com/Anilll-coder/codeorbit/main/install.sh | sh
```

**Windows (PowerShell)**

```powershell
irm https://raw.githubusercontent.com/Anilll-coder/codeorbit/main/install.ps1 | iex
```

Or from a clone: `./install.sh` / `.\install.ps1`.

The installer puts CodeOrbit in its own virtualenv so it can never disturb your
system Python, adds a `codeorbit` launcher to your PATH, and offers to pull the
Ollama model. Re-running upgrades in place; `--uninstall` / `-Uninstall` removes
it and leaves your project indexes alone.

Needs Python 3.10+, and [Ollama](https://ollama.com) for `codeorbit ask`.

<details>
<summary>Install knobs</summary>

| sh | PowerShell | meaning |
|---|---|---|
| `CODEORBIT_HOME` | `-InstallDir` | where the virtualenv lives |
| `CODEORBIT_BIN` | `-BinDir` | where the launcher goes |
| `CODEORBIT_NO_MODEL=1` | `-NoModel` | skip the model download |
| `CODEORBIT_REF` | `-Ref` | branch or tag to install |

</details>

<details>
<summary>Developing on it instead</summary>

```sh
python -m venv .venv
.venv/Scripts/activate        # Windows;  source .venv/bin/activate on Unix
pip install -e .
```

</details>

## Use

```bash
codeorbit index .                      # build the graph
codeorbit status                       # what the index holds

codeorbit search Console               # find symbols by name
codeorbit show Console.print           # source + callers + callees
codeorbit callers render               # who calls this
codeorbit impact Segment --depth 2     # blast radius of a change
codeorbit entry                        # what the most code depends on
codeorbit dead                         # definitions nothing calls

codeorbit ask "How does Console.print render output?"

codeorbit review                       # review your change with its blast radius
codeorbit review --base main           # ...against a branch
codeorbit review --staged --no-llm     # just the risk table, no model

codeorbit audit                        # bugs and security issues, ranked by reach
codeorbit audit -s high --explain      # high severity only, explained by the model
codeorbit why make_user normalize      # the call path between two symbols

codeorbit embed                        # enable search by meaning (one time)

codeorbit fix -s high                  # propose fixes, verified, preview only
codeorbit fix -s high --apply --test   # ...write them, gated on the test suite

codeorbit viz                          # interactive graph as a standalone HTML file
codeorbit viz --focus Segment          # ...centred on one symbol
codeorbit viz --format mermaid         # a diagram for a report
```

Point any command at another project with `-p/--path`.

## Architecture

```
files
  -> scanner.py      walk the repo, skip vendored/generated, respect git
  -> extract/        tree-sitter -> symbols, call sites, imports  (pass 1)
  -> indexer.py      write nodes + pending calls into SQLite
  -> resolve.py      bind call sites to real definitions           (pass 2)
  -> query.py        callers / callees / impact / search / source
  -> context.py      question -> relevant subgraph -> grounded prompt
  -> review.py       diff -> changed symbols -> blast radius -> review prompt
  -> rules.py        29 security / bug / quality patterns
  -> audit.py        findings, attributed to symbols and ranked by reach
  -> semantic.py     symbol embeddings, fused with keyword search by RRF
  -> fixer.py        propose a fix for one symbol, splice it, never trust it
  -> verify.py       the gates a fix must survive before it may touch a file
  -> viz.py          self-contained interactive HTML, or Mermaid
  -> llm.py          Ollama, streaming, local only
  -> cli.py          the commands above
```

**Two passes, not one.** Pass 1 records every call site as *pending*, because a
call can name something defined in a file that has not been parsed yet. Pass 2
binds them once every definition is known. Keeping unresolved calls in a table
rather than discarding them makes recall measurable — you can query exactly what
the resolver missed.

**Resolution is ranked and labelled.** Most trustworthy first: same file →
`self`/`this` receiver → explicit import → unique project-wide name → ambiguous.
The first three are recorded as `exact`, the rest as `heuristic`, and a name
shared by more than three definitions is dropped rather than linked everywhere.
The distinction is surfaced to the model as `[uncertain]`, so a guess is never
presented as a fact. **A wrong edge is worse than a missing one, because the
model believes it.**

## Schema

| table | holds |
|---|---|
| `files` | path, language, content hash, LOC |
| `nodes` | symbols: module, class, function, method |
| `edges` | `contains`, `calls`, `imports`, `extends` — with call-site line and `exact`/`heuristic` |
| `pending_calls` | call sites awaiting resolution; the resolver's miss list |
| `imports` | per-file imports, used to scope name matching |
| `nodes_fts` | FTS5 index over names and docstrings |

## Languages

Python and JavaScript/JSX. Adding one is a new file in `extract/` plus an entry
in `extract/__init__.py`.

## Measured

Indexing [`rich`](https://github.com/Textualize/rich) (100 files, 38,615 lines):

| | |
|---|---|
| symbols | 1,193 |
| edges | 5,381 |
| in-project call sites resolved | 61% |
| index time | seconds |

The unresolved remainder is almost entirely calls that genuinely leave the
project (`append`, `len`, `Path`, library methods). Those correctly get no edge.

### Performance on constrained hardware

Measured on 7.3 GB RAM with **no usable GPU** — `ollama ps` reports phi4-mini
resident at 3.7 GB running **100% CPU**. Retrieval itself is instant (SQLite);
every number below is the model.

| model | output cap | time |
|---|---|---|
| `phi4-mini` | uncapped (~200 words) | 103 s – 190 s |
| `phi4-mini` | 120 tokens | **50 s** |
| `qwen2.5:0.5b` | 120 tokens | 21 s |

**Generation speed is the bottleneck — roughly 1–2 tokens/sec on CPU — so total
time tracks how much the model *says*, not how much context it was given.**
Three runs of one identical query took 190 s, 123 s and 103 s purely on
answer-length variance, all with the model already warm.

Two consequences:

- **Capping output is the main lever.** `--max-tokens` defaults to 320; drop it
  to ~150 for quick lookups. Retrieval size (`-n`) is a much weaker lever than
  it first appears.
- **`phi4-mini` is the floor for usable answers.** `qwen2.5:0.5b` is 2.5×
  faster and worthless here — asked what `resolve_project` does, it echoed the
  function signature and stopped. Speed below this size buys nothing.

## Reviewing a change

A diff says what changed. It cannot say what the change *reaches* — and that is
where the risk is. `codeorbit review` maps changed line ranges onto the symbols
that own them, then pulls each one's callers, blast radius and covering tests out
of the graph before asking the model to review:

```
2 file(s) changed  +81 -2  |  5 symbol(s) touched
| symbol                  | blast | callers | tests |
| codeorbit.query.callers | 2     | 1       | none  |
```

The two facts that matter most there — *one caller depends on this* and *nothing
tests it* — are not in the diff. They come from the graph. Changed symbols are
ordered by what they reach rather than by how many lines moved, and changed files
that are not indexed are named rather than silently skipped.

`--no-llm` prints the risk table alone, which is instant.

## Finding problems, ranked by what they reach

A linter hands you a flat list. On a real codebase that list is unusable,
because it will not say which of 400 findings to fix first. The graph answers
that: every finding is attributed to the symbol containing it, and ordered by
severity, then by how much code transitively depends on that symbol, then by
whether any test reaches it.

On `rich` (38.6k lines), the top finding sits inside a symbol 82 other symbols
depend on. The same rule firing in dead code sorts to the bottom, which is
where it belongs.

`--explain` then asks the local model which findings are real. That step earns
its keep: the scan flags `hashlib.sha1` in this project's own scanner, and a
regex cannot know that it is hashing file contents for change detection rather
than protecting a password. A reviewer can.

## Search by meaning as well as by name

Keyword search answers "where is `parse_and_extract`". It cannot answer "where
do we decide which files to skip" - none of those words appear in the code.
`codeorbit embed` embeds each symbol (name, signature, docstring and a slice of
body) with `nomic-embed-text`, locally.

The unit is the **symbol**, not a file chunk. That is the whole point: a
semantic hit is only the door, and the neighbourhood around it still comes from
real call edges. Meaning finds the door; structure walks the building.

Two things here were measured and both were wrong on the first attempt, which is
worth recording:

1. `nomic-embed-text` requires `search_document:` / `search_query:` prefixes.
   Without them, similarity is materially worse.
2. Blending keyword and similarity scores **additively does nothing**. Cosine
   similarities sit in a narrow 0.55-0.65 band, so the blend applies a near
   constant offset that ranks nothing, and the keyword scores simply win. The
   hybrid returned the keyword-only answer for every question tried.

The fix is Reciprocal Rank Fusion, which throws the scores away and fuses only
each list's *order*. After it, `scanner.scan` surfaces for "where do we decide
which files to skip" where it previously did not appear at all - and questions
that do name an identifier still rank it first, so nothing regressed. Compare
the two yourself with `ask --no-semantic`.

## Fixing, without trusting the model

A 3.8B model on CPU produces confident nonsense some of the time, so
`codeorbit fix` never applies anything on the strength of the model's
confidence. Each candidate is spliced into a **copy** of the project and put
through gates, cheapest first:

| gate | rejects |
|---|---|
| `syntax` | the edit does not parse (`ast.parse`, or `node --check`) |
| `symbol` | the symbol we set out to fix no longer exists - the worst failure mode, a "fix" that deletes the function |
| `tests` | the project's own suite goes red (opt-in, `--test`) |

Only a candidate that passes every gate is even offered. Writing is opt-in
(`--apply`) and always leaves a `.orig` backup. `apply()` itself re-checks the
verdict and raises rather than write an unverified fix.

The model is given the smallest job that can work - rewrite ONE symbol, return
only that symbol - because asking a small model for a whole-file rewrite loses
unrelated code. Three real failure modes are handled rather than rejected: a
method returned flush-left is re-indented, imports the model prepends are
hoisted to the file's import block instead of being spliced mid-file (and not
duplicated if already present), and `--test` checks the suite is green *before*
any change so a pre-existing failure is not blamed on the model.

## Seeing the graph

`codeorbit viz` writes one HTML file that embeds its data and its own force
layout and makes **zero external requests** - no CDN, no fonts, no network. A
viewer that needed an internet connection would contradict the whole premise;
this one can be emailed or opened from a USB stick.

Two views, because they answer different questions. `modules` is one node per
file with import edges: *how is this project shaped?* `symbols` is functions and
methods with call edges, optionally centred on one symbol via `--focus`: *what
does this touch?* Click any node to isolate it and list its neighbours;
`--format mermaid` emits a diagram to paste into a report instead.

## Tests

```bash
pip install -e ".[dev]"
pytest
```

59 tests, no mocks: each builds a small project on disk, indexes it, and asserts
against the real graph. Mocking the parser would only test the mock, and the
fixer's tests never call the model - what has to hold is that everything
*around* the model is safe regardless of what it returns.

Writing them found two real bugs, both now pinned by a test:

- `nodes_fts` is an external-content FTS5 table, so the `ON DELETE CASCADE` that
  removes a deleted file's symbols does not touch the search index - deleted
  symbols stayed searchable forever. Fixed with a delete trigger.
- `json.dumps` does not escape `<`, so a symbol named `</script>` closed the
  script block in a generated viewer and everything after it parsed as HTML.
  Every string on that page comes from the codebase being analysed, so that is
  attacker-controlled input the moment you point this at a repository you did
  not write. Now escaped to `<`.

## Status

Complete and working end to end: index → resolve → query → graph-grounded local
answers, review of a change, ranked auditing, verified fix generation, and an
interactive graph viewer.

Languages are Python and JavaScript. The known limits are honest ones: static
parsing cannot follow dynamic dispatch (decorator-registered and dict-dispatched
functions show up as uncalled), and answer latency is bounded by CPU generation
speed rather than by anything the graph does.

## Prior art

The design — two-pass extraction, the node/edge taxonomy, labelling synthesized
edges as heuristic — follows
[colbymchenry/codegraph](https://github.com/colbymchenry/codegraph) (MIT), a
TypeScript implementation of the same idea, studied as a reference. This is an
independent Python implementation and shares no code with it.
