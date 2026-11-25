# Pre-registration: agent test on SWE-bench Verified

Written and committed **before any run of this test**. The commit that adds this
file is the timestamp. Anything done differently later is listed under
*Deviations* at the end, with the reason. Every result is reported, whatever it
shows.

## Question

Does giving a coding agent CodeOrbit's MCP tools make it name the function a
real bug fix changed more often, and at what cost in tokens?

**Hypothesis (H1):** with CodeOrbit, the agent names a function the fix changed
on more issues than without it. Secondary: it uses fewer input tokens per issue
it gets right.

## Issues

- **Dataset:** SWE-bench Verified (`SWE-bench/SWE-bench_Verified`, test split,
  downloaded file SHA-256 `36208eba...0a94`).
- **Excluding every issue that is also in SWE-bench Lite** (Lite file SHA-256
  `59059054...3268`), so no issue has been used by any earlier run of ours:
  407 remain.
- **Main sample:** 100 of the 407, chosen by
  `--split verified --exclude-split lite --sample 100 --seed 3` (listed below).
- **Pilot:** 3 issues outside the main sample, only to check the pipeline:
  `psf__requests-1921`, `pytest-dev__pytest-7205`, `pytest-dev__pytest-10081`.
  The pilot's outcomes are not results and change nothing below.

## Conditions

Each issue is given to the agent twice, each time in a fresh conversation, in
alternating order:

- **without:** the agent's ordinary read-only tools (search text, read a file,
  list a directory).
- **with:** the same tools plus CodeOrbit's MCP tools (`explore`, `search`,
  `node`, `outline`, `impact`) and the server's instructions.

The agent must answer with up to 5 locations written `path:Function` or
`path:Class.method`. It cannot run code or tests.

## Models

1. **qwen3:14b** (open, local) through Ollama on Kaggle (2 x NVIDIA T4), with
   `scripts/agent_eval.py`: temperature 0, fixed seed, thinking off, 16,384-token
   context, at most 20 turns, at most 12,000 characters per tool result.
2. **OpenCode** (a real, open-source coding agent; version recorded in the run)
   with the free hosted model `opencode/big-pickle`, run non-interactively
   (`opencode run`) on an ordinary laptop with `scripts/cli_agent_eval.py --agent
   opencode`. Tools: OpenCode's read, grep, glob and list in *without*, plus
   CodeOrbit's MCP server in *with*; edit, bash, web, sub-agents and the rest are
   denied; at most 20 agentic steps (`steps: 20`). Two differences from the
   Kaggle run, both fixed in advance: CodeOrbit runs **without embeddings** (a
   laptop CPU cannot embed these repositories in reasonable time), so its tools
   rank with name search and BM25 only; and the provider does not publish which
   model `big-pickle` is, so the result is reported as "a free hosted model"
   with its OpenCode identifier. A service error (an unavailable model, a
   network failure) is retried once and then stops the run to be resumed; it is
   never recorded as a failed answer. If the model is withdrawn before the run
   finishes, the result covers the issues completed, in the fixed order of the
   sample, and is reported as such.

CodeOrbit version: the commit recorded in each run's `meta.json`; no change to
CodeOrbit's retrieval or tools is made between the start of the first main run
and the end of the last.

## Measures

- **Primary:** a function the fix changed is among the agent's answers.
  Gold functions: the outermost function or method containing each line the
  fix changed, at the issue's commit (`scripts/swebench.py`), as in all our
  SWE-bench work.
- **Secondary:** the changed function as the first answer; all changed
  functions named; the changed file named; input tokens per issue (the whole
  conversation, every turn, as an API bills it); input tokens per success;
  output tokens; turns; seconds; whether an answer was given.

## Analysis

- Per model, *with* vs *without* on the same issues, paired.
- Primary: exact two-sided McNemar test on the issues where exactly one
  condition succeeds; significance level 0.05. One test per model; no other
  test is used to claim H1.
- Secondary: paired bootstrap 95% intervals (10,000 resamples) for differences;
  results split by whether the issue names a gold function (named / unnamed)
  are reported as exploratory.
- Issues that fail setup (checkout, indexing) are excluded from both conditions
  and listed by ID. No issue is excluded for any other reason.

## Second sample: other languages (added before any run on it)

The 100 issues above are all Python. To test the same question in the other
languages CodeOrbit supports, a second sample is run by **both agents** with the
same conditions, measures and analysis, reported separately (one McNemar test
per agent per sample; the two samples are never pooled):

- **Dataset:** SWE-bench Multilingual (`SWE-bench/SWE-bench_Multilingual`, test
  split, downloaded file SHA-256 `7d0c845f...`). It was used before only for
  retrieval scores, where nothing was tuned on it; no agent has seen it.
- **Sample:** 100 of its 300 issues, chosen by
  `--split multilingual --sample 100 --seed 3`: Rust 15, JavaScript/TypeScript
  15, PHP 15, Ruby 15, C/C++ 14, Java 13, Go 12, and 1 whose fix touches no
  supported code file (excluded under the setup rule); 32 projects. Listed below.
- **Pilot:** 3 issues outside the sample, only to check the pipeline:
  `gin-gonic__gin-3741` (Go), `google__gson-2061` (Java), `sharkdp__bat-1892`
  (Rust).

### Multilingual sample (100 issues)

1. `apache__lucene-11760` (Java)
2. `apache__lucene-12022` (Java)
3. `apache__lucene-12196` (Java)
4. `apache__lucene-13301` (Java)
5. `astral-sh__ruff-15330` (Rust)
6. `astral-sh__ruff-15356` (Rust)
7. `astral-sh__ruff-15394` (Rust)
8. `astral-sh__ruff-15443` (Rust)
9. `axios__axios-4731` (JS/TS)
10. `axios__axios-4738` (JS/TS)
11. `axios__axios-6539` (JS/TS)
12. `babel__babel-15649` (JS/TS)
13. `babel__babel-16130` (JS/TS)
14. `briannesbitt__carbon-2665` (PHP)
15. `briannesbitt__carbon-2752` (PHP)
16. `briannesbitt__carbon-3041` (PHP)
17. `briannesbitt__carbon-3103` (PHP)
18. `caddyserver__caddy-5870` (Go)
19. `caddyserver__caddy-6345` (Go)
20. `facebook__docusaurus-10309` (JS/TS)
21. `fastlane__fastlane-19304` (Ruby)
22. `fastlane__fastlane-20642` (Ruby)
23. `fastlane__fastlane-20958` (Ruby)
24. `fastlane__fastlane-21857` (Ruby)
25. `fluent__fluentd-3328` (Ruby)
26. `fluent__fluentd-3616` (Ruby)
27. `fluent__fluentd-3640` (Ruby)
28. `fluent__fluentd-4311` (Ruby)
29. `fluent__fluentd-4655` (Ruby)
30. `fmtlib__fmt-2317` (C/C++)
31. `fmtlib__fmt-3158` (C/C++)
32. `fmtlib__fmt-3248` (C/C++)
33. `gin-gonic__gin-2755` (Go)
34. `gohugoio__hugo-12204` (no supported code file)
35. `gohugoio__hugo-12343` (Go)
36. `gohugoio__hugo-12448` (Go)
37. `gohugoio__hugo-12768` (Go)
38. `google__gson-1093` (Java)
39. `google__gson-2479` (Java)
40. `hashicorp__terraform-34580` (Go)
41. `hashicorp__terraform-34900` (Go)
42. `hashicorp__terraform-35611` (Go)
43. `jekyll__jekyll-8167` (Ruby)
44. `jekyll__jekyll-9141` (Ruby)
45. `jqlang__jq-2598` (C/C++)
46. `jqlang__jq-2650` (C/C++)
47. `jqlang__jq-2658` (C/C++)
48. `laravel__framework-48573` (PHP)
49. `laravel__framework-48636` (PHP)
50. `laravel__framework-52451` (PHP)
51. `laravel__framework-53696` (PHP)
52. `laravel__framework-53914` (PHP)
53. `mrdoob__three.js-25687` (JS/TS)
54. `mrdoob__three.js-26589` (JS/TS)
55. `nushell__nushell-12950` (Rust)
56. `php-cs-fixer__php-cs-fixer-7523` (PHP)
57. `php-cs-fixer__php-cs-fixer-7875` (PHP)
58. `php-cs-fixer__php-cs-fixer-8256` (PHP)
59. `phpoffice__phpspreadsheet-3469` (PHP)
60. `phpoffice__phpspreadsheet-4114` (PHP)
61. `phpoffice__phpspreadsheet-4214` (PHP)
62. `preactjs__preact-3345` (JS/TS)
63. `preactjs__preact-3562` (JS/TS)
64. `preactjs__preact-3567` (JS/TS)
65. `preactjs__preact-3763` (JS/TS)
66. `preactjs__preact-4152` (JS/TS)
67. `preactjs__preact-4182` (JS/TS)
68. `projectlombok__lombok-3215` (Java)
69. `projectlombok__lombok-3312` (Java)
70. `projectlombok__lombok-3350` (Java)
71. `projectlombok__lombok-3479` (Java)
72. `projectlombok__lombok-3486` (Java)
73. `projectlombok__lombok-3571` (Java)
74. `projectlombok__lombok-3674` (Java)
75. `prometheus__prometheus-11859` (Go)
76. `prometheus__prometheus-12874` (Go)
77. `prometheus__prometheus-15142` (Go)
78. `redis__redis-10068` (C/C++)
79. `redis__redis-11279` (C/C++)
80. `redis__redis-11734` (C/C++)
81. `redis__redis-12272` (C/C++)
82. `redis__redis-12472` (C/C++)
83. `redis__redis-13115` (C/C++)
84. `rubocop__rubocop-13393` (Ruby)
85. `rubocop__rubocop-13431` (Ruby)
86. `rubocop__rubocop-13503` (Ruby)
87. `rubocop__rubocop-13579` (Ruby)
88. `sharkdp__bat-2393` (Rust)
89. `sharkdp__bat-2835` (Rust)
90. `tokio-rs__axum-734` (Rust)
91. `tokio-rs__axum-2096` (Rust)
92. `tokio-rs__tokio-4384` (Rust)
93. `tokio-rs__tokio-4867` (Rust)
94. `tokio-rs__tokio-6603` (Rust)
95. `tokio-rs__tokio-6724` (Rust)
96. `uutils__coreutils-6682` (Rust)
97. `uutils__coreutils-6731` (Rust)
98. `valkey-io__valkey-1499` (C/C++)
99. `valkey-io__valkey-1842` (C/C++)
100. `vuejs__core-11589` (JS/TS)

## Follow-up

A larger follow-up may be run after this test. If it is, it uses issues outside
this sample, is pre-registered separately before it starts, and is reported in
full whatever this test or it shows. Its results are never pooled with these
100 issues.

## Main sample (100 issues)

1. `astropy__astropy-7606`
2. `astropy__astropy-7671`
3. `astropy__astropy-14096`
4. `astropy__astropy-14309`
5. `astropy__astropy-14539`
6. `django__django-10097`
7. `django__django-10973`
8. `django__django-10999`
9. `django__django-11066`
10. `django__django-11211`
11. `django__django-11239`
12. `django__django-11265`
13. `django__django-11551`
14. `django__django-11734`
15. `django__django-11820`
16. `django__django-12155`
17. `django__django-12262`
18. `django__django-12304`
19. `django__django-12406`
20. `django__django-12419`
21. `django__django-13012`
22. `django__django-13112`
23. `django__django-13128`
24. `django__django-13516`
25. `django__django-13820`
26. `django__django-14007`
27. `django__django-14170`
28. `django__django-14311`
29. `django__django-14349`
30. `django__django-14725`
31. `django__django-15022`
32. `django__django-15037`
33. `django__django-15161`
34. `django__django-15277`
35. `django__django-15375`
36. `django__django-15499`
37. `django__django-15916`
38. `django__django-16032`
39. `django__django-16136`
40. `django__django-16263`
41. `django__django-16560`
42. `django__django-16612`
43. `django__django-16662`
44. `django__django-16877`
45. `django__django-16899`
46. `django__django-16901`
47. `django__django-17029`
48. `django__django-17084`
49. `matplotlib__matplotlib-20488`
50. `matplotlib__matplotlib-20826`
51. `matplotlib__matplotlib-21568`
52. `matplotlib__matplotlib-22871`
53. `matplotlib__matplotlib-24177`
54. `matplotlib__matplotlib-25122`
55. `matplotlib__matplotlib-25960`
56. `psf__requests-2931`
57. `psf__requests-6028`
58. `pydata__xarray-2905`
59. `pydata__xarray-3305`
60. `pydata__xarray-3677`
61. `pydata__xarray-4695`
62. `pydata__xarray-6938`
63. `pylint-dev__pylint-4970`
64. `pytest-dev__pytest-5262`
65. `pytest-dev__pytest-5631`
66. `pytest-dev__pytest-5787`
67. `pytest-dev__pytest-6197`
68. `pytest-dev__pytest-6202`
69. `pytest-dev__pytest-10051`
70. `scikit-learn__scikit-learn-10908`
71. `scikit-learn__scikit-learn-11310`
72. `scikit-learn__scikit-learn-11578`
73. `scikit-learn__scikit-learn-12682`
74. `scikit-learn__scikit-learn-13124`
75. `scikit-learn__scikit-learn-13328`
76. `scikit-learn__scikit-learn-14496`
77. `scikit-learn__scikit-learn-14629`
78. `scikit-learn__scikit-learn-14710`
79. `scikit-learn__scikit-learn-25973`
80. `scikit-learn__scikit-learn-26194`
81. `sphinx-doc__sphinx-7440`
82. `sphinx-doc__sphinx-7748`
83. `sphinx-doc__sphinx-7757`
84. `sphinx-doc__sphinx-7910`
85. `sphinx-doc__sphinx-9230`
86. `sphinx-doc__sphinx-9258`
87. `sphinx-doc__sphinx-9281`
88. `sphinx-doc__sphinx-9320`
89. `sympy__sympy-13877`
90. `sympy__sympy-14248`
91. `sympy__sympy-14976`
92. `sympy__sympy-15809`
93. `sympy__sympy-16766`
94. `sympy__sympy-17318`
95. `sympy__sympy-18211`
96. `sympy__sympy-20916`
97. `sympy__sympy-21596`
98. `sympy__sympy-22080`
99. `sympy__sympy-23413`
100. `sympy__sympy-23534`

## Deviations

- Before any run: the second agent was changed from Claude Code (Sonnet) to
  OpenCode with a free hosted model, because the project's aim is to help free
  and small models, and a subscription model's usage limits would have stretched
  200 sessions over days. Claude Code remains supported by the harness
  (`--agent claude`) but is not part of this test.
- After the qwen3:14b pilot, before its main run: in the pilot, qwen3:14b gave no
  answer in all 3 *without* runs, and the harness could not show why. On the
  forced final turn, a model that writes its locations as text instead of
  calling `finish` is now scored on those locations (stop reason `text_answer`),
  in both conditions alike, and the final message's text is saved with every
  run. The earlier qwen3:8b run used the previous rule (text on the final turn
  counted as no answer).
- Kaggle quota (7.4 h left): the qwen3:14b pilots were not re-run after that fix,
  and the Multilingual pilot was not run for qwen3:14b; both main samples run
  back to back in one session under one time budget. Issues not reached are
  resumed later in the same fixed order and reported as such.
