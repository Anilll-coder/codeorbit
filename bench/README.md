# Benchmarks

Everything a reported number depends on lives here, so a result can be traced
back to the questions, the repository version and the code that produced it.

| path | what | committed |
|---|---|---|
| `datasets/*.json` | questions with hand-verified target symbols, one file per repository | yes |
| `runs/<utc>-<dataset>/` | one benchmark run: `meta.json`, `summary.json`, per-question JSONL/CSV | yes, clean runs only |
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
and Internet on, choose `MODE`, then Save Version -> Save & Run All. It stops
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
