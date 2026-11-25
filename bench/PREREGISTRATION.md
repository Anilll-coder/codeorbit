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
2. **Claude Code** with a Claude Sonnet model (frontier), run non-interactively
   (`claude -p`) with its built-in read-only tools (Read, Grep, Glob) in
   *without*, plus CodeOrbit's MCP server in *with*; at most 20 turns; the exact
   model ID and Claude Code version are recorded in the run. If plan usage limits
   stop this run early, the result covers the issues completed, in the fixed
   order of the sample, and is reported as such.

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
- Secondary: paired bootstrap 95% intervals (2,000 resamples) for differences;
  results split by whether the issue names a gold function (named / unnamed)
  are reported as exploratory.
- Issues that fail setup (checkout, indexing) are excluded from both conditions
  and listed by ID. No issue is excluded for any other reason.

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

None yet.
