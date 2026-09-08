# CodeGraph

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

```bash
python -m venv .venv
.venv/Scripts/activate        # Windows;  source .venv/bin/activate on Unix
pip install -e .
```

Requires Python 3.10+ and [Ollama](https://ollama.com) for the `ask` command:

```bash
ollama pull phi4-mini
```

## Use

```bash
codegraph index .                      # build the graph
codegraph status                       # what the index holds

codegraph search Console               # find symbols by name
codegraph show Console.print           # source + callers + callees
codegraph callers render               # who calls this
codegraph impact Segment --depth 2     # blast radius of a change
codegraph entry                        # what the most code depends on
codegraph dead                         # definitions nothing calls

codegraph ask "How does Console.print render output?"
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

Measured on 7.3 GB RAM, CPU-only (no usable GPU), `phi4-mini`:

| retrieved context | answer time |
|---|---|
| 5,187 chars (1 symbol) | **32 s** |
| 11,476 chars (3 symbols) | **4 m 21 s** |

2.2× the context costs 8× the time — prompt ingestion dominates and degrades
sharply under memory pressure. **Context budget is the main performance lever
here, not model choice.** Hence the conservative default of 2 symbols; raise it
with `-n` when the answer needs more and you can wait.

## Status

Working end to end: index → resolve → query → graph-grounded local answers.

Not built yet: AI code review over a diff, bug/security detection, automated fix
generation with verification, and an interactive graph visualisation.

## Prior art

The design — two-pass extraction, the node/edge taxonomy, labelling synthesized
edges as heuristic — follows
[colbymchenry/codegraph](https://github.com/colbymchenry/codegraph) (MIT), a
TypeScript implementation of the same idea, studied as a reference. This is an
independent Python implementation and shares no code with it.
