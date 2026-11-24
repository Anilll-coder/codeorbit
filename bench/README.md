# Benchmarks

Everything a reported number depends on lives here, so a result can be traced
back to the questions, the repository version and the code that produced it.

| path | what | committed |
|---|---|---|
| `datasets/*.json` | questions with hand-verified target symbols, one file per repository | yes |
| `runs/<utc>-<dataset>/` | one benchmark run: `meta.json`, `summary.json`, per-question JSONL/CSV | yes, clean runs only |
| `agent_runs/<utc>-agent-<model>/` | one real-agent run: `meta.json`, `runs.jsonl`, `summary.json` | yes |
| `swebench/*.jsonl` | downloaded SWE-bench tasks | no - `swebench.py download` |
| `edges/*.csv` | call sites sampled for edge-accuracy labelling | yes, once labelled |
| `repos/` | repositories checked out at their dataset pin | no - re-fetch from the pin |

## Retrieval benchmark

```bash
python scripts/bench_fetch.py --embed          # check out + index + embed pinned repos
python scripts/benchmark.py --dataset bench/datasets/<name>.json \
    --tokenizer hf:microsoft/Phi-4-mini-instruct --measure-index
python scripts/bench_report.py --markdown      # tables from saved runs, never by hand
```

Run from a clean tree with Ollama up. `meta.json` records `dirty` and
`semantic_live`; `bench_report.py` ignores dirty runs and flags runs with
embeddings off.

### Adding a repository

1. Pick a commit and write `datasets/<name>.json`:

   ```json
   {
     "name": "click",
     "repo": {
       "url": "https://github.com/pallets/click",
       "default_path": "bench/repos/click",
       "pin": {"commit": "<full 40-character SHA>"}
     },
     "questions": [
       {"id": "click-n01", "kind": "named", "text": "How does Group.add_command work?",
        "target": "core.Group.add_command", "verified": false}
     ]
   }
   ```

   `kind` is `named` when the question contains an identifier from the code and
   `unnamed` when it does not. `target` is the symbol's qname exactly as
   `codeorbit` stores it (check with `codeorbit search`).

2. Read the source and confirm each target answers its question, then set
   `"verified": true`. Unverified questions are skipped unless
   `--include-unverified` is passed: a target guessed by a tool and scored by a
   tool is the benchmark grading itself.
3. Write questions before running any arm, and do not edit them after seeing
   results. Keep a mix of `named` and `unnamed`.

## SWE-bench: bug localisation on real issues

```bash
python scripts/swebench.py download --split lite         # or verified, multilingual
python scripts/swebench.py run --split lite --sample 60 --seed 0 --embed            # development
python scripts/swebench.py run --split lite --exclude-sample 60 --exclude-seed 0 --embed  # held out
python scripts/bench_report.py bench/runs/<run> --versus fusion,hybrid
```

| split | tasks | languages | used for |
|---|---|---|---|
| `lite` | 300 | Python, 12 projects | the main result: 60 development + 240 held out |
| `verified` | 500 (93 also in Lite) | Python, 12 projects | a larger second Python set |
| `multilingual` | 300 | Go, Rust, Java, C/C++, Ruby, PHP, JavaScript, TypeScript; 41 projects | the languages besides Python |

Gold targets are the outermost function or method containing each line the
fix's patch changed, at the issue's commit. A patch that only adds new
functions or edits module-level code has no function target; it counts for
file-level scores only. `--versus A,B` compares two arms task by task: what
each finds that the other misses, and the "either" ceiling a perfect
combination of the two could reach.

Fusion's weights are tuned offline on the development runs only:
`python scripts/tune_fusion.py bench/runs/<dev run>`. Freeze the result in
`benchmark.FUSION`, then judge it on the held-out tasks.

## Real-agent test: with vs without CodeOrbit

Retrieval scores say whether the right code reaches the model; this says
whether an agent uses it. A local tool-calling model is given each issue and
the repository, and must name the functions to change. Every task runs twice
from a fresh conversation, with the same model, prompt and limits:

- **without**: `grep`, `read_file`, `list_dir` - the tools agents use today
- **with**: the same three plus CodeOrbit's MCP tools (`explore`, `search`,
  `node`, `outline`, `impact`) and the server's instructions, called in
  process

```bash
python scripts/agent_eval.py run --model qwen3:8b --split lite --embed
python scripts/agent_eval.py report bench/agent_runs/<run>
```

Recorded per run: whether a function the fix changed is in the answer (and
first in it), its file, input tokens as an API bills them (the whole
conversation, every turn), output tokens, tokens the tools returned, turns and
time. The report pairs the two conditions per task: an exact McNemar test on
success and paired bootstrap 95% intervals on every difference. Answers are
scored again from the saved text on every report, so a scoring fix applies to
finished runs without running the model again.
[`kaggle_agent.ipynb`](kaggle_agent.ipynb) runs two models side by side on
Kaggle's two GPUs.

## SWE-bench on a GPU (Google Colab)

Embeddings are the slow step on a CPU - hours for a repository the size of
django. [`colab_swebench.ipynb`](colab_swebench.ipynb) runs
`scripts/swebench.py --embed` on Colab's free T4 GPU: open it in Colab
(`File -> Upload notebook`), pick a T4 runtime, and run the cells top to
bottom. It clones whatever is pushed to `main`, so push first. Results go
to `MyDrive/codeorbit-runs` and survive a disconnect (`--resume`); copy a
run folder into `bench/runs/` here to commit it.

### Unattended on Kaggle

[`kaggle_swebench.ipynb`](kaggle_swebench.ipynb) does the same on a Kaggle GPU
with no browser open: upload it as a Kaggle notebook, set Accelerator to a GPU
and Internet on, choose `SPLIT`, `MODE` and `ARMS`, then Save Version -> Save &
Run All, and stop the editor session (it uses GPU quota too). It stops
itself after `MAX_HOURS` (Kaggle allows 12) and resumes from a previous
version's output via `RESUME_FROM`. Results: the Output tab's
`codeorbit-runs.zip`.

## Edge accuracy

```bash
python scripts/edge_eval.py sample <repo path> --n 150
# fill the gold column in bench/edges/<repo>.csv by reading the code
python scripts/edge_eval.py score bench/edges/<repo>.csv
```

`gold` is the correct target's qname, `-` if the call leaves the project, or
`?` if it cannot be decided. Label from the code, not from `predicted`.
