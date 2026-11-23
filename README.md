<div align="center">

<img src="assets/logo.svg" alt="" width="104" height="104">

# CodeOrbit

**Local-First Code Intelligence with Graph-Grounded LLMs**

A structural graph of your repository — **137 languages, one index** — served to any
MCP agent, or answered by a model that runs on your machine.

[![Languages](https://img.shields.io/badge/languages-137-e2a13f?style=flat-square&labelColor=0a0b0d)](#languages)
[![Extensions](https://img.shields.io/badge/extensions-207-e2a13f?style=flat-square&labelColor=0a0b0d)](#languages)
[![Python](https://img.shields.io/badge/python-3.10%2B-e2a13f?style=flat-square&logo=python&logoColor=f2b658&labelColor=0a0b0d)](#install)
[![MCP](https://img.shields.io/badge/MCP-server-e2a13f?style=flat-square&labelColor=0a0b0d)](#using-it-from-claude-code-cursor-or-any-mcp-agent)
[![tree-sitter](https://img.shields.io/badge/parser-tree--sitter-9b9ca4?style=flat-square&labelColor=0a0b0d)](#languages)
[![Network calls](https://img.shields.io/badge/network%20calls-none-7fb992?style=flat-square&labelColor=0a0b0d)](#what-it-is)

**[Website](https://anilll-coder.github.io/codeorbit/)**&nbsp; · &nbsp;**[Install](#install)**&nbsp; · &nbsp;**[Languages](#languages)**&nbsp; · &nbsp;**[For agents](#does-it-actually-save-the-agent-any-tokens)**&nbsp; · &nbsp;**[Commands](https://anilll-coder.github.io/codeorbit/#usage)**

</div>

---

```sh
curl -fsSL https://raw.githubusercontent.com/Anilll-coder/codeorbit/main/install.sh | sh
codeorbit index .
codeorbit install-mcp            # your agent now reads the graph, not guesses
```

<table>
<tr>
<td width="25%" align="center"><h3>137</h3><sub>LANGUAGES READ</sub></td>
<td width="25%" align="center"><h3>61%</h3><sub>CALL SITES BOUND</sub></td>
<td width="25%" align="center"><h3>&minus;61%</h3><sub>AGENT TOKENS</sub></td>
<td width="25%" align="center"><h3>0</h3><sub>NETWORK CALLS</sub></td>
</tr>
<tr>
<td align="center"><sub>Three tiers of extraction, from hand-written to inferred straight out of the grammar.</sub></td>
<td align="center"><sub>Measured on <code>rich</code>: 1,193 symbols, 5,381 edges. The rest genuinely leaves the project.</sub></td>
<td align="center"><sub>Over a repeated session, against the flat response format. Estimated at 4 chars/token.</sub></td>
<td align="center"><sub>Indexing is on disk, the graph is served over loopback and stdio. No keys, no telemetry.</sub></td>
</tr>
</table>

---

## What it is

CodeOrbit parses a repository with tree-sitter, builds a structural knowledge
graph of its symbols and their relationships in SQLite, and answers questions
about the code using a **local** LLM through Ollama.

CodeOrbit itself never calls out: indexing runs on disk, the graph is served over
local stdio, and there are no API keys and no telemetry required to use it.
Answer fully offline with a model in Ollama, or hand the same graph to Claude
Code, Cursor, or any agent you already use — in which case what you send
travels to whatever that agent talks to. `codeorbit agent` can optionally
drive a cloud model itself (`--provider openai` or `--provider anthropic`) if
you choose to point it at one and set the key yourself; left alone, it is
Ollama, which is the only choice that needs no network at all.

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

Putting it on PATH means editing your user PATH in the registry on Windows, and
appending one fenced `export PATH=...` block to your shell rc file (`.bashrc`,
`.zshrc`, `.profile`, or fish's `config.fish`) elsewhere. **Open a new terminal
afterwards**, or `. ~/.bashrc` — the shell that ran the installer does not see
the change. Pass `CODEORBIT_NO_PATH=1` to skip that edit and manage PATH
yourself.

Needs Python 3.10+, and [Ollama](https://ollama.com) for `codeorbit ask`.

<details>
<summary>Install knobs</summary>

| sh | PowerShell | meaning |
|---|---|---|
| `CODEORBIT_HOME` | `-InstallDir` | where the virtualenv lives |
| `CODEORBIT_BIN` | `-BinDir` | where the launcher goes |
| `CODEORBIT_NO_MODEL=1` | `-NoModel` | skip the model download |
| `CODEORBIT_NO_PATH=1` | — | do not edit shell rc files to extend PATH |
| `CODEORBIT_REF` | `-Ref` | branch or tag to install |

</details>

<details>
<summary>Developing on it instead</summary>

```sh
python -m venv .venv
.venv/Scripts/activate        # Windows;  source .venv/bin/activate on Unix
pip install -e ".[dev]"
```

`pyproject.toml` is the only source of truth for dependencies. There is no
committed `requirements.txt` - a hand-maintained second copy of the same list
goes stale exactly like hand-written docs do. If some part of your toolchain
specifically wants one, generate it:

```sh
python scripts/gen_requirements.py        # requirements.txt, core deps
python scripts/gen_requirements.py --dev  # ...plus the dev extra
```

It is gitignored on purpose; regenerate it rather than committing it.

</details>

## Use

```bash
codeorbit index .                      # build the graph
codeorbit status                       # what the index holds
codeorbit langs                        # which languages this install can read

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
codeorbit review --pr 42               # review a GitHub PR against its own head
codeorbit review --pr 42 --post        # ...and offer to post it back

codeorbit audit                        # bugs and security issues, ranked by reach
codeorbit audit -s high --explain      # high severity only, explained by the model
codeorbit why make_user normalize      # the call path between two symbols

codeorbit embed                        # enable search by meaning (one time)

codeorbit fix -s high                  # propose fixes, verified, preview only
codeorbit fix -s high --apply --test   # ...write them, gated on the test suite

codeorbit viz                          # explore the graph in a browser, served locally
codeorbit viz --focus Segment          # ...centred on one symbol
codeorbit viz --out graph.html         # a standalone file instead of a server
codeorbit viz --format mermaid         # a diagram for a report

codeorbit mcp                          # serve the graph over MCP to any agent
codeorbit install-mcp                  # print the config to register it
codeorbit agent                            # a session with a local model
codeorbit agent "is load safe to change?"   # ...or one question, one answer

codeorbit uninstall --dry-run          # see exactly what removal would touch
codeorbit uninstall                    # remove CodeOrbit (indexes are kept)
```

Every command takes a project path, in whichever position reads better:

```bash
codeorbit -p ~/code/app audit      # before the command
codeorbit audit -p ~/code/app      # after it
codeorbit index ~/code/app         # index and status also take it positionally
```

With none given, it works on the current directory.

`codeorbit` with no arguments prints the help, and the help is grouped: **Build
the graph**, **Explore**, **Ask questions**, **Review and fix**, **Connect an AI
agent**, **Manage this install**. Twenty commands in one flat alphabetical list
told you nothing about where to start.

## Architecture

```
files
  -> langs.py        which file is which language, and what grammar reads it
  -> scanner.py      walk the repo, skip vendored/generated, respect git
  -> extract/        tree-sitter -> symbols, call sites, imports  (pass 1)
       python_ex.py      hand-written, for the languages this is
       javascript_ex.py  measured on
       typescript_ex.py
       generic.py        one walker for every other grammar, driven by a
                         table or, failing that, by the grammar itself
       embedded.py       .vue / .svelte / .astro: parse the script block
  -> indexer.py      write nodes + pending calls into SQLite
  -> resolve.py      bind call sites to real definitions           (pass 2)
  -> query.py        callers / callees / impact / search / source
  -> context.py      question -> relevant subgraph -> grounded prompt
  -> review.py       diff -> changed symbols -> blast radius -> review prompt
  -> github.py       the GitHub API, read a PR and post one comment
  -> pr.py           a PR reviewed against a graph of its own head
  -> rules.py        66 security / bug / quality patterns, per language family
  -> audit.py        findings, attributed to symbols and ranked by reach
  -> semantic.py     symbol embeddings, fused with keyword search by RRF
  -> fixer.py        propose a fix for one symbol, splice it, never trust it
  -> verify.py       the gates a fix must survive before it may touch a file
  -> viz.py          graph data, self-contained HTML, or Mermaid
  -> server.py       local HTTP server for the explorer
  -> browser.py      open a URL on any platform, or say why it could not
  -> budget.py       spend an agent's context deliberately, not by accident
  -> web/            the explorer itself: canvas renderer + layout worker
  -> mcp_server.py   the graph as MCP tools, for Claude Code / Cursor / etc
  -> mcp_config.py   write CodeOrbit into an agent's own MCP config file
  -> agent.py        drives those same tools itself - any provider, not fixed
  -> providers.py    the openai-compatible and anthropic backends behind
                     --provider; translated to one shape agent.py depends on
  -> llm.py          Ollama client, streaming, local only - what `ask` uses
  -> cli.py          the commands above
```

`site/` is the project page, deployed to
[GitHub Pages](https://anilll-coder.github.io/codeorbit/) by
`.github/workflows/static.yml` on every push to `main`. It carries an
[interactive command reference](https://anilll-coder.github.io/codeorbit/#usage):
filter by command or by flag, click one, and read its options and copyable
examples.

That reference is **generated from the CLI**, not written by hand:

```bash
python scripts/gen_commands.py          # rewrite site/commands.json
python scripts/gen_commands.py --check  # fail if it is stale
```

A test asserts the committed file matches the live command objects, so adding a
command or renaming a flag without regenerating fails the suite rather than
quietly shipping documentation that is wrong.

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

```bash
codeorbit langs              # what this install can read
codeorbit langs --verbose    # the whole list
```

**137 languages across 207 file extensions**, from one registry in `langs.py`.
Extraction has three tiers, tried in that order:

| | tier | count | how it reads a file |
|---|---|---|---|
| **1** | Hand-written | **4** | Python, JavaScript/JSX, TypeScript/TSX. The languages this is measured on, where a tuned extractor catches what a generic one cannot: PEP 257 docstrings, an arrow function bound to a `const`, `extends B implements C`. |
| **2** | Table-driven | **30** | A `Spec` per language in `extract/generic.py` naming which node types are definitions, calls and imports. Go, Rust, Java, C, C++, C#, Ruby, PHP, Swift, Kotlin, Scala, Dart, Elixir, Solidity, Lua, shell and the rest. |
| **3** | Inferred | **103** | `infer_spec` reads node types out of the grammar itself and classifies them by name. Grammars written by different people over a decade still converge hard on `function_definition`, `class_declaration`, `call_expression`, `import_declaration` — so a language nobody here has looked at still yields a graph. Gleam has no table entry and needs none. |

Each tier-2 entry earned itself against a real file. Go methods needed the
receiver (`Server.Start`, not `Start`); a Rust `impl` had to fold into its
struct rather than duplicate it; C hides a function's name two declarators deep;
Dart has no call node at all; Elixir has no *definition* node, because `def`
there is a macro call, so it gets a hook.

`.vue`, `.svelte`, `.astro` and `.html` go through a fourth path: the wrapper is
blanked out line-for-line and the script block handed to the JavaScript or
TypeScript extractor, so a single-file component indexes its logic *and* its
line numbers still point at the real lines.

Adding a language is normally one line in `SUFFIX`. A language whose grammar
spells things unusually gets a `Spec` entry; a language with no definition node
at all — Elixir, where `def` is a macro call — gets a hook.

> [!IMPORTANT]
> **What this does not do.**
>
> - **A language with no grammar installed is skipped and named**, never indexed
>   empty. A file row with no symbols is indistinguishable from an empty file,
>   and that is a lie the graph then repeats to the model.
> - **Inference is weaker than a table entry.** Nim and Crystal yield symbols and
>   imports but *no call edges*, because their call nodes are named in ways the
>   patterns do not reach. That limit is pinned by a test rather than papered
>   over — inventing edges is worse than missing them, since the model believes
>   them.
> - **Name resolution never crosses a language family.** TypeScript, JavaScript,
>   `.vue` and `.svelte` compile together and call each other; Go and Python do
>   not, and binding a Go `Load` to a Python `load` produced call edges that were
>   pure coincidence.

## Measured

Indexing [`rich`](https://github.com/Textualize/rich) 15.0.0 (100 files, 38,615 lines):

| | |
|---|---|
| symbols | 1,193 |
| edges | 5,381 |
| in-project call sites bound to a target | 61% (2,757 of 4,488) |
| full index time | 0.8 s (0.7 s parse, 0.1 s resolve) |
| peak RSS while indexing | 39 MB |
| database size | 1.4 MB |

The unresolved remainder is almost entirely calls that genuinely leave the
project (`append`, `len`, `Path`, library methods). Those correctly get no edge.
61% is a *binding rate*, not an accuracy: it says how many call sites got an
edge, not how many of those edges are right. Edge precision and recall need a
hand-labelled sample and have not been measured yet.

### Does the graph actually retrieve better?

Run it yourself: `python scripts/benchmark.py`. Questions live in
[`bench/datasets/`](bench/datasets/), pinned to the repository version they were
written against. The script runs every arm below, reports Recall@1/5/10, MRR and
nDCG@10, and writes per-question records plus run metadata to `bench/runs/`.
Pass `--tokenizer tiktoken:o200k_base` or `hf:<model>` for real token counts,
and `--measure-index` for index time, peak RSS and database size.

**What is measured.** Whether an arm surfaces the code that answers a question.
Each of 12 questions has a ground-truth symbol whose source actually contains
the answer, verified against `rich` by hand before any arm ran. No LLM judge, no
rubric, no run-to-run variance. Answer quality would need a judge, and a judge
running on a 3.8B model measures the judge.

**Every arm gets the same 6,000-character budget.** Two context metrics are
reported: *found* - the target's name or definition appears in what the model
would see - and the stricter *code* - the target's definition itself appears.
The difference matters: a callers list that names a symbol is not its source.

Results on `rich` 15.0.0, embeddings live, tokens counted with the phi4-mini
tokenizer ([run record](https://github.com/Anilll-coder/codeorbit/tree/8a001d901f6360b2fc77700a0f6b80af27a24858/bench/runs/20261115T130656Z-rich)):

| arm | MRR@10 | Recall@5 | found | code | tokens |
|---|---|---|---|---|---|
| grep - question identifiers, regions ranked by hit count | 0.07 | 17% | 17% | 17% | 1,232 |
| BM25 over function/class chunks | 0.43 | 67% | 67% | 58% | 1,061 |
| dense - embeddings only | 0.58 | 67% | 67% | 67% | 1,052 |
| hybrid - BM25 + dense, RRF | 0.54 | 67% | 67% | 67% | 956 |
| CodeOrbit, embeddings off | 0.50 | 58% | 67% | 58% | 1,398 |
| CodeOrbit, graph expansion off | 0.58 | 67% | 67% | 67% | 1,058 |
| **CodeOrbit** | **0.58** | **67%** | **75%** | **67%** | 1,325 |

**What this does and does not show.**

- Every structured arm beats grep by a wide margin. Grep finds a *name*
  everywhere - every call site, import and docstring mention - and its budget
  fills with references before it reaches the definition.
- **CodeOrbit does not beat a standard dense or hybrid retriever on this set.**
  Its ranking is identical to embeddings alone. Its one extra *found* hit
  (*"how does a piece of text get broken across several lines"* →
  `Text.wrap`) is the target's name appearing in a callers/callees list, not
  its code; on the stricter *code* metric CodeOrbit, dense and hybrid tie at
  67%.
- What graph expansion adds here is pointers to neighbouring symbols. Whether
  those pointers improve a model's *answers* is not measured yet.
- Questions that name nothing in the code remain the open problem: no arm
  retrieves the code for any of the four.
- **12 questions is a pilot, not evidence.** Every arm's 95% bootstrap interval
  spans roughly 40 percentage points. A multi-repository question set is the
  next step before any general claim.

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

### Reviewing a pull request

```bash
codeorbit review --pr 42                 # review PR #42, print it here
codeorbit review --pr 42 --repo o/n      # ...when it is not in origin's repo
codeorbit review --pr 42 --no-llm        # just the risk table
codeorbit review --pr 42 --post          # ...and offer to post it (asks first)
```

**The PR is reviewed against a graph of its own head, not of your checkout.**
This is the part that makes the difference between a useful review and a
confident wrong one. If your index is built from `main` and the PR moves a
function, every caller and blast-radius number describes code the PR has already
changed. So `--pr` fetches `pull/N/head`, checks it out into a throwaway git
worktree, indexes *that*, reviews against it, and removes it - including when
the review fails. Your own checkout is never touched, never re-indexed, and
never left on another branch.

Needs a token in `GITHUB_TOKEN` or `GH_TOKEN` (or `gh auth login`, which it will
read from). Read access is enough unless you use `--post`.

**A pull request is untrusted input.** Its title, description and existing
comments are written by whoever opened it, and they go into a prompt. They are
fenced with a per-run nonce so the text cannot close its own block and start
issuing instructions, they are labelled as claims rather than evidence, and the
system prompt tells the model to report any attempt to direct the review rather
than obey it. That reduces the risk; it does not remove it. Read what it posts.

**`--post` is the one thing here that writes to the world.** It is off by
default, it asks before every post, it prints the PR URL first, and it always
posts `event=COMMENT` - never `APPROVE` or `REQUEST_CHANGES`. An automated
approval can satisfy branch protection, and a 3.8B local model's opinion must
never be able to do that.

Note the honesty about the network: `--pr` reads the GitHub API, so unlike the
rest of CodeOrbit it does call out. What it sends is a PR number and a token.
What comes back - your diff and your source - stays local unless you use a
cloud-backed model, and `--post` publishes whatever the model wrote.

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

`codeorbit viz` starts a small server on 127.0.0.1 and opens the graph in your
browser. It takes the first free port from a short list (7373 upward), so the
URL stays the same across restarts and an open tab survives a reload. Ctrl-C
stops it.

It binds to loopback and nothing else, deliberately. The index holds your source
and the detail panel serves it verbatim, so nothing on your network should be
able to reach it.

A server rather than a file because a file has to decide everything up front.
Every node, edge and function body has to be inlined before the browser opens,
which caps how much of a repository the page can hold and rules out the
interesting operations: search the whole index, open a neighbourhood, read a
body. Those are now requests, and the page ships only what you are looking at.

Two views, because they answer different questions. `modules` is one node per
file with import edges: *how is this project shaped?* `symbols` is functions and
methods with call edges, optionally centred on one symbol via `--focus`: *what
does this touch?* Click a node to dim everything that is not adjacent to it and
read its callers, callees and source. The left rail is built from what your
index actually contains: a single-language project gets no language legend, and
the directory and kind filters list your directories and your kinds.

**Still zero external requests** - no CDN, no fonts, no npm. That constraint is
the whole premise, and it is why the layout and the renderer are written here
rather than pulled from a package.

Drawing is on a canvas, and the three things that decide whether a large graph
is usable are handled explicitly: layout runs in a Web Worker using Barnes-Hut
approximation, so a tick is `O(n log n)` rather than `O(n^2)` and never competes
with the frame; nothing outside the viewport is drawn; and detail drops out as
you zoom away, with labels capped and edges fading out entirely below a
threshold where they carry no information. Hit testing goes through a uniform
grid rather than scanning every node on every mouse move.

Two escape hatches from the server:

```bash
codeorbit viz --out graph.html      # the old self-contained file, still one file
codeorbit viz --format mermaid      # a diagram to paste into a report
codeorbit viz --port 9000           # a specific port, an error if it is taken
```

The standalone file is still the right tool for sending someone a picture of a
codebase. The server is the tool for exploring one.

### Opening the browser, on machines that are not a laptop

`viz` is the command most likely to be run somewhere other than a developer's
desktop — a server, a container, a WSL shell, an SSH session. It used to call
`webbrowser.open(url)` and throw the answer away, which failed three separate
ways and reported all three as success:

| failure | what actually happened |
|---|---|
| **It blocked** | `webbrowser` picks a *console* browser — w3m, links, lynx — when no graphical one is registered, and runs it in the foreground waiting for it to exit. The callback that opens the browser ran before `serve_forever()`, so the terminal browser took over the terminal and the server it was pointed at never started answering. The command looked hung with its own URL printed above it. |
| **It did nothing** | Headless, containerised or over SSH without X forwarding, `webbrowser.open` returns `False`. Nobody looked, so nothing was printed and the user waited for a window that was never coming. |
| **It looked in the wrong place** | Under WSL it searched for a Linux browser inside the VM while the browser the user wanted was on the Windows side, reached through `wslview` or `powershell.exe`. |

`browser.py` now picks the opener per platform, spawns it **detached** so
nothing can block, and returns a result saying what happened:

| platform | opener |
|---|---|
| macOS | `open` |
| Windows | `cmd /c start ""` |
| Linux | `xdg-open` → `gio open` → `gnome-open` → `kde-open` |
| WSL | `wslview` → `powershell.exe Start-Process` → `cmd.exe start` |
| headless | none — prints the URL and the `ssh -L` that reaches it |

`on_ready` runs on its own thread, so even a slow opener cannot delay serving.
On a machine with no way to open a window it says so:

```
Could not open a browser (headless).
No graphical session was detected (no DISPLAY or WAYLAND_DISPLAY). If you are
on a remote machine, forward the port and open the URL locally:
ssh -L 7373:127.0.0.1:7373 <host>
```

`CODEORBIT_NO_BROWSER=1` turns the attempt off entirely, for CI and scripts.

The exported page colours nodes from what the graph actually contains rather
than a fixed list of two languages, so a Go or Rails repo gets a legend that
describes it instead of one naming languages it does not have.

## Using it from Claude Code, Cursor, or any MCP agent

```bash
codeorbit install-mcp                     # writes the config for Claude Code
codeorbit install-mcp -a cursor           # ...or Cursor
codeorbit install-mcp -a windsurf         # ...or Windsurf
codeorbit install-mcp -a vscode           # ...or VS Code
codeorbit install-mcp -a claude-desktop   # ...or the Claude desktop app
codeorbit install-mcp --print             # show it for any OTHER MCP client
codeorbit install-mcp --remove            # take it back out
```

| agent | file it writes |
|---|---|
| `claude` (default) | `.mcp.json`, project-scoped, safe to commit |
| `cursor` | `.cursor/mcp.json` (or `~/.cursor/mcp.json` with `--global`) |
| `windsurf` | `.windsurf/mcp.json` (or `~/.codeium/windsurf/mcp_config.json` global) |
| `vscode` | `.vscode/mcp.json` — the one target with a genuinely different file shape: `{"servers": {...}}`, not `{"mcpServers": {...}}` |
| `claude-desktop` | the desktop app's own config. There is no per-project file for it at all, so `--global` is implied whether you pass it or not |

Five named targets is not the ceiling. The graph is served over **standard
MCP** — any client that speaks the protocol can use it, named here or not.
`--print` shows the exact `mcpServers` (or `servers`, for VS Code) block to
paste into whatever config file your client expects; adding a sixth named
target is one entry in `mcp_config.py`'s `TARGETS` table, not a new code path.

Running the write form **merges** — other MCP servers already in the file are
left exactly as they were, a backup is taken before the first write, running
it twice changes nothing, and a file it cannot parse is refused rather than
overwritten, because that file may hold your other servers. Then restart the
agent.

The command it writes is an absolute path to a real executable. An agent spawns
the server as a bare subprocess with no shell and no PATH lookup of its own, so
a plain `codeorbit` resolves to nothing — and on Windows the entry point is
`codeorbit.exe` while the installed launcher is `codeorbit.cmd`, neither of which
a bare name finds.

`--path` is baked into the config, so one entry serves one project. For several,
add one entry per project under different names, or omit `--path` and pass
`project_path` on each tool call.

**Cursor needs one more step: approval.** Its agent refuses to load an MCP
server the user has not approved, and keys that approval to a hash of the exact
config entry. Start `cursor-agent` in the project and approve `codeorbit` when
it asks, or run `/mcp`. Two things about this are worth knowing, because the
failure does not look like what it is:

- `cursor-agent mcp list` does **not** check approvals. A server that lists
  fine there, tools and all, can still be refused inside a session with
  `MCP server "codeorbit" has not been approved`. Listing is not connecting.
- Anything that rewrites the entry — a reinstall to a new location, a change to
  `--path` — changes the hash and revokes the approval. Cursor asks again;
  until you answer, sessions report the server as unavailable. `install-mcp`
  will not rewrite an entry that only *spells* its paths differently, precisely
  so re-running it does not cost you the approval.

If a session still reports it as unavailable, `cursor-agent --approve-mcps`
starts one with the gate off, which tells you whether approval was the problem.
Treat it as a diagnostic rather than a fix: it approves **every** MCP server the
project configures, not just CodeOrbit, and only for that run.

Eight tools are exposed: `explore` (the primary one), `search`, `node`,
`outline`, `impact`, `path`, `audit`, `overview`. `outline` lists a file's
functions and classes with their lines and signatures but no code - about 7%
of the file's tokens - so an agent can see what a file holds before reading
any of it.

**This is the one path where your code can leave the machine.** CodeOrbit still
makes no outbound call of its own — it speaks stdio to a client on the same
machine — but a tool result is source code, and a cloud-backed client sends what
it receives to its own provider. That is the trade for a stronger model. For a
private codebase, `codeorbit ask` keeps everything local, and so does
`codeorbit agent` at its default `--provider ollama` - the same trade only
applies there if you explicitly choose `--provider openai` or `--provider
anthropic`, and even then only to the questions you ask it, not to indexing.

**These return context, not answers, and that is the point.** `codeorbit ask`
runs a local 3.8B model because a developer at a terminal has none to hand. An
agent connecting over MCP already has a far stronger one - it does not want
phi4-mini's answer, it wants the surgical context that lets its own model answer
well. So `explore` hands back the subgraph: the symbols, their real source, who
calls them, what they call.

Three rules shape the responses, and they are about how agents actually behave:

1. **Be sufficient.** An agent falls back to reading files the moment a tool's
   answer is not enough, and every fallback costs a turn. A symbol comes back
   with its full body *and* its call neighbourhood in one response.
2. **Never send the agent to read a file.** If more is needed, point at another
   tool.
3. **An expected condition is not an error.** "Not indexed" and "no such symbol"
   return ordinary results carrying guidance, because a tool that raises errors
   trains an agent to stop calling it - after which none of this gets used.

Rule 3 was learned the hard way: a transient read error escaped as a protocol
error and the agent received only `Error executing tool codeorbit_explore`.

### Does it actually save the agent any tokens?

That is the whole claim — a tool call has to cost less than the file read it
replaces — and the first version did not clear the bar convincingly. Every one
of four picks got a flat 120-line body plus its full call neighbourhood, clipped
at 24,000 characters. Roughly six thousand tokens a call, most of it spent on
the third and fourth pick, which are the least likely to be what was asked.

Three things were wasting it, and `budget.py` fixes each:

| | waste | fix |
|---|---|---|
| **01** | **A flat cap.** Every pick got the same 120 lines, however unlikely it was to be the answer. | Budgets taper down the ranking. The symbol that matters is not shortened at all; the tail costs a fraction. |
| **02** | **The same code twice in one response.** Retrieval routinely returns a class *and* one of its own methods, whose bodies overlap exactly. | The second becomes a pointer at the first. |
| **03** | **The same code in every response.** A session is `explore`, then `node`, then `impact`, over the same few symbols. | After two full sends the body becomes a citation — with `detail="full"` as the escape hatch, which is what makes the elision safe rather than merely cheap. |

Measured over six questions against this repository, estimated at four
characters per token:

| | est. tokens | change |
|---|--:|--:|
| old format, six calls | 13,307 | — |
| new format, first pass | 10,805 | **−18%** |
| the same six questions, third time in the session | 5,131 | **−61%** |
| `detail="signatures"` | 4,067 | **−69%** |

Passes one and two cost the same by design — a body gets two full sends before
it is elided, because an agent's context is compacted without warning and a
symbol asked about twice running is one it is actively working on.

The numbers are estimates and say so: `estimate_tokens` is a budgeting ratio at
four characters per token, not a tokeniser. The direction is what is claimed,
and the *shape* is the point — the saving grows the longer the session runs,
which is exactly when an agent's context is under pressure.

Clipping is still there as a backstop but should never fire. Deciding what to
leave out before paying to format it beats truncating a response already built.
And a body that *is* cut short says so, with the line it stopped at and how to
get the rest — silently truncating mid-function teaches an agent the tool is
unreliable and to go read the file, which costs more than it ever saved.

## Driving it yourself - any model, through the same MCP tools

No cloud agent needed - `codeorbit agent` drives the same MCP tools any
external agent would, over the same protocol, itself. By default that means an
Ollama model, fully local, no key, nothing leaves the machine. It is not
limited to Ollama: `--provider` points the same loop at any OpenAI-compatible
server (LM Studio, vLLM, llama.cpp's own server, or the real OpenAI API) or at
Claude directly, without changing what it does - the graph, the tool-calling
loop, the context budgeting below are all identical regardless of who answers.

```bash
codeorbit agent                               # a session: ask, read, ask again
codeorbit agent .                             # the same, on this directory
codeorbit agent "what does resolve_project do and what calls it?"   # one shot
codeorbit agent --check                       # which local models can call tools

codeorbit agent --provider openai --model gpt-4o-mini
codeorbit agent --provider openai --model local-model \
  --base-url http://localhost:1234/v1         # a local server instead
codeorbit agent --provider anthropic --model claude-sonnet-5
```

`--provider` is the only thing that changes. `openai` covers anything that
speaks the OpenAI chat-completions shape - not just OpenAI's own API - because
LM Studio, vLLM, llama.cpp's server, text-generation-webui and OpenRouter all
agree on that wire format; only `--base-url` differs between them. `anthropic`
talks to Claude's Messages API directly, which is different enough (content
blocks, no `tool` role, the system prompt as its own field) that the
translation happens once, here, rather than being left to each caller.

**Credentials are read from the environment only** - `OPENAI_API_KEY` /
`ANTHROPIC_API_KEY` by default, or a name you choose with `--api-key-env` - and
never accepted as CLI text, the same rule this project holds for every other
secret. A local OpenAI-compatible server that takes no key at all (LM Studio, a
bare llama.cpp server) is a first-class case: no key set means no
`Authorization` header sent, not a refusal to start. Anthropic is the one
provider that does require a key, and says so - with where to get one - before
it makes any request.

With no question it opens an interactive session: one question at a time, each
answered against the graph, with the previous couple of exchanges carried
forward so "what calls it?" resolves against what you just asked about. `/help`
lists the commands, `/clear` forgets the conversation, `/exit` or Ctrl-D leaves.

A path is not a question. `codeorbit agent .` opens a session on that directory
rather than spending a round having the model reply that it is ready and waiting.

**Ollama is started for you.** If it is installed but not running, the agent
starts it in the background and carries on, because "open another terminal and
run a daemon" is a step you were always going to take. If it is not installed at
all, it says where to get it instead of trying.

Two things keep the session fast and the answers readable, and both were
learned from a transcript that listed fourteen symbols and then stopped
mid-word:

- **The context is bounded.** One MCP server is held open for the whole session
  rather than respawned per question; tool results are clipped before the model
  sees them; the oldest ones are dropped when the conversation outgrows its
  budget; and between questions only the prose of previous turns is carried,
  never their tool output. Without that, every question is slower than the last.
- **The answer is bounded too.** The model is told to name the two or three
  things that matter rather than read a tool result back, and when it does hit
  the token limit the answer is labelled as cut off rather than simply ending
  mid-sentence. `--max-tokens` raises the ceiling.

While it works you get a spinner that says what it is waiting on - `thinking`,
or `reading the graph: codeorbit_explore` - with the seconds counted next to it.
A round on a CPU model is tens of seconds, and a terminal that prints nothing
for that long is indistinguishable from one that has hung; the elapsed counter
is what separates "slow" from "broken".

**Tool calling is a property of the model's chat template, not of Ollama**, and a
model without one does not fail loudly - it invents a plausible result and states
it as fact. Measured on this machine:

| model | calls tools | usable |
|---|---|---|
| `phi4-mini` | no | fabricates results instead |
| `qwen2.5:0.5b` | yes | too weak - passed a symbol name as `project_path` |
| `llama3.2:3b` | yes | **yes** - ~2 GB, correct answers |

So capability is probed before the loop starts and an incapable model is refused
rather than trusted. `ollama pull llama3.2:3b` is the smallest model that both
calls tools and uses them correctly here. Expect a few minutes per question on
CPU; the agent loop is several model round-trips, not one.

## Tests

```bash
pip install -e ".[dev]"
pytest
```

483 tests, no mocks: each builds a small project on disk, indexes it, and asserts
against the real graph. Mocking the parser would only test the mock, and the
fixer's tests never call the model - what has to hold is that everything
*around* the model is safe regardless of what it returns.

`test_polyglot.py` does the same for each language: one real file per language,
asserting that a class comes out as a class, a method as a method *under its
type*, a call as a call and an import as an import. Without it a language
degrades silently to a module node with nothing in it, which looks exactly like
an empty file. The three tests that skip are the ones needing
`tree-sitter-language-pack`, so the suite still passes on an install without it.

Writing them found two real bugs, both now pinned by a test:

- `nodes_fts` is an external-content FTS5 table, so the `ON DELETE CASCADE` that
  removes a deleted file's symbols does not touch the search index - deleted
  symbols stayed searchable forever. Fixed with a delete trigger.
- `json.dumps` does not escape `<`, so a symbol named `</script>` closed the
  script block in a generated viewer and everything after it parsed as HTML.
  Every string on that page comes from the codebase being analysed, so that is
  attacker-controlled input the moment you point this at a repository you did
  not write. Now escaped to `<`.

A third was found by running the CLI end to end rather than by a test: a
JavaScript `import { load } from "./svc"` bound to a *Python* `svc.load` on the
name alone, and was reported as an `exact` call edge. Project-wide name matching
is now scoped to one language.

## Status

Complete and working end to end: index → resolve → query → graph-grounded local
answers, review of a change, ranked auditing, verified fix generation, an
interactive graph viewer, an MCP server for external agents, and an agent of its
own over those same tools - Ollama by default, or any OpenAI-compatible server
or Claude directly via `--provider`.

Around 137 languages, with three tiers of fidelity — see
[Languages](#languages). The known limits are honest ones, and the tool says
which apply rather than hiding them:

- Static parsing cannot follow dynamic dispatch. Decorator-registered and
  dict-dispatched functions show up as uncalled, and a missing edge always means
  "not statically visible", never "does not happen".
- Outside the hand-written and tabled languages, extraction is inferred from the
  grammar. It finds definitions and imports reliably and call edges less so; a
  few grammars (Nim, Crystal) yield no call edges at all.
- A language with no grammar installed is skipped and named, not indexed empty.
- Answer latency for `ask` is bounded by CPU generation speed rather than by
  anything the graph does.

## Uninstalling

```bash
codeorbit uninstall --dry-run   # show what would go, change nothing
codeorbit uninstall             # ask, then remove
codeorbit uninstall --index     # also drop this project's .codeorbit/
```

It removes its own virtualenv, the launcher, and the PATH entry the installer
added. It does not remove your source, your project indexes (unless `--index`),
or Ollama.

Three guards, because this is the one command that deletes things it did not
create:

- **It refuses outside a virtualenv.** If CodeOrbit was pip-installed into the
  system Python then `sys.prefix` *is* that Python, and removing it would take
  the interpreter with it. There it tells you to `pip uninstall codeorbit`.
- **It never removes source.** With an editable install it names your checkout
  as protected and leaves it alone.
- **It asks first**, and `--dry-run` shows the whole plan without touching
  anything.

Windows cannot delete a running `.exe`, and the launcher lives inside the
directory being removed - so there the final step is handed to a short detached
process that waits for the command to exit first.

The installer scripts still work too: `./install.sh --uninstall` or
`.\install.ps1 -Uninstall`.

## Prior art

The design — two-pass extraction, the node/edge taxonomy, labelling synthesized
edges as heuristic — follows
[colbymchenry/codegraph](https://github.com/colbymchenry/codegraph) (MIT), a
TypeScript implementation of the same idea, studied as a reference. This is an
independent Python implementation and shares no code with it.

---

<div align="center">

<sub>Local-first, always. No API keys leave the process; no telemetry is sent.</sub>

**[Website](https://anilll-coder.github.io/codeorbit/)** &nbsp;·&nbsp;
**[Install](#install)** &nbsp;·&nbsp;
**[Languages](#languages)** &nbsp;·&nbsp;
**[Issues](https://github.com/Anilll-coder/codeorbit/issues)**

</div>
