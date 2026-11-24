"""The benchmark's scoring code. A metric bug would misreport every result."""
import importlib.util
import json
import math
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("benchmark", ROOT / "scripts" / "benchmark.py")
bench = importlib.util.module_from_spec(_spec)
sys.modules["benchmark"] = bench     # dataclasses look their module up here
_spec.loader.exec_module(bench)


def _row(rank, hit=False, kind="named"):
    return {"rank": rank, "hit_ctx": hit, "def_ctx": hit, "chars": 400,
            "tokens": 100, "ms": 1.0, "kind": kind, "arm": "a"}


def test_ranking_metrics_by_hand():
    m = bench.metrics([_row(1, True), _row(3, True), _row(None)])
    assert m["recall@1"] == pytest.approx(1 / 3)
    assert m["recall@5"] == pytest.approx(2 / 3)
    assert m["recall@10"] == pytest.approx(2 / 3)
    assert m["mrr@10"] == pytest.approx((1 + 1 / 3) / 3)
    assert m["ndcg@10"] == pytest.approx((1 + 1 / math.log2(4)) / 3)
    assert m["hit_ctx"] == pytest.approx(2 / 3)


def test_rank_beyond_depth_counts_as_a_miss():
    units = [bench.Unit("", qname=f"m.f{i}") for i in range(20)]
    assert bench.first_rank(units, "m.f3") == 4
    assert bench.first_rank(units, "m.f15") is None


def test_graph_units_match_by_qname_not_substring():
    u = bench.Unit("def wrap(self): ...", qname="text.Text.wrap_lines")
    assert not bench.unit_matches(u, "text.Text.wrap")


def test_grep_units_match_on_definition_only():
    assert bench.unit_matches(bench.Unit("    def wrap(self, width):"), "text.Text.wrap")
    assert not bench.unit_matches(bench.Unit("x = text.wrap(width)"), "text.Text.wrap")


def test_def_ctx_is_stricter_than_hit_ctx():
    callers_only = "called by:\n  - console.Console._collect_renderables (console.py:10)"
    target = "console.Console._collect_renderables"
    assert bench.contains_target(callers_only, target)
    assert not bench.defines(callers_only, target)


def test_code_tokens_split_identifiers():
    toks = bench.code_tokens("render_lines getStyleName")
    assert {"render_lines", "render", "lines", "getstylename", "get", "style", "name"} <= set(toks)


def test_pack_respects_budget_and_never_returns_empty():
    units = [bench.Unit("a" * 40), bench.Unit("b" * 40)]
    assert len(bench.pack(units, 50)) <= 50
    assert bench.pack([bench.Unit("c" * 500)], 100) == "c" * 100


def test_bootstrap_ci_brackets_the_mean():
    rows = [_row(1, True)] * 6 + [_row(None)] * 6
    lo, hi = bench.bootstrap_ci(rows, lambda r: float(r["hit_ctx"]))
    assert lo < 0.5 < hi


def test_shipped_dataset_is_well_formed():
    for path in (ROOT / "bench" / "datasets").glob("*.json"):
        data, qs = bench.load_dataset(path)
        assert qs and data["repo"]["pin"]
        assert {q.kind for q in qs} <= {"named", "unnamed"}
        assert all(q.target.count(".") >= 1 for q in qs), path
        json.dumps(data)


def _load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_edge_scoring_by_hand():
    ev = _load("edge_eval")
    rows = [
        {"predicted": "a.f", "resolution": "exact", "gold": "a.f"},          # TP
        {"predicted": "a.g", "resolution": "heuristic", "gold": "a.f"},      # FP + FN
        {"predicted": "a.f | b.f", "resolution": "heuristic", "gold": "a.f"},  # TP + FP
        {"predicted": "", "resolution": "unbound", "gold": "-"},             # correct no-edge
        {"predicted": "c.len", "resolution": "heuristic", "gold": "-"},      # FP
        {"predicted": "", "resolution": "unbound", "gold": "a.h"},           # FN
        {"predicted": "a.f", "resolution": "exact", "gold": "?"},            # excluded
        {"predicted": "a.f", "resolution": "exact", "gold": ""},             # unlabelled
    ]
    m = ev.score_rows(rows)["overall"]
    assert (m["tp"], m["fp"], m["fn"], m["sites"]) == (2, 3, 2, 6)
    assert m["precision"] == pytest.approx(2 / 5)
    assert m["recall"] == pytest.approx(2 / 4)
    assert ev.score_rows(rows)["heuristic"]["fp"] == 3


def test_unverified_questions_load_as_unverified(tmp_path):
    p = tmp_path / "d.json"
    p.write_text(json.dumps({"name": "d", "repo": {"pin": {"commit": "x"}}, "questions": [
        {"id": "1", "kind": "named", "text": "q", "target": "m.f", "verified": False},
        {"id": "2", "kind": "named", "text": "q", "target": "m.g"}]}), encoding="utf-8")
    _, qs = bench.load_dataset(p)
    assert [q.verified for q in qs] == [False, True]


PATCH = """diff --git a/pkg/mod.py b/pkg/mod.py
--- a/pkg/mod.py
+++ b/pkg/mod.py
@@ -1,4 +1,5 @@
 import os
+import sys
 
 
 class Box:
@@ -6,7 +7,7 @@ class Box:
     def size(self):
         def inner():
-            return 1
+            return 2
         return inner()
 
     def other(self):
diff --git a/docs/notes.txt b/docs/notes.txt
--- a/docs/notes.txt
+++ b/docs/notes.txt
@@ -1 +1 @@
-old
+new
diff --git a/pkg/new.py b/pkg/new.py
--- /dev/null
+++ b/pkg/new.py
@@ -0,0 +1 @@
+x = 1
"""

SOURCE = """import os


class Box:

    def size(self):
        def inner():
            return 1
        return inner()

    def other(self):
        return 3
"""


def test_patched_lines_use_original_line_numbers():
    swe = _load("swebench")
    lines = swe.patched_lines(PATCH)
    assert lines["pkg/mod.py"] == {1, 8}     # insert after line 1; line 8 removed
    assert "pkg/new.py" not in lines         # new files have no original
    assert lines["docs/notes.txt"] == {1}


def test_gold_targets_roll_nested_functions_up_to_the_method(tmp_path):
    from codeorbit import db
    from codeorbit.indexer import index_project
    from codeorbit.resolve import resolve_project

    swe = _load("swebench")
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "mod.py").write_text(SOURCE, encoding="utf-8")
    index_project(tmp_path)
    resolve_project(tmp_path)
    conn = db.connect(tmp_path)
    targets, files = swe.gold_targets(conn, PATCH)
    conn.close()
    assert files == ["pkg/mod.py"]                       # notes.txt is not indexed
    assert len(targets) == 1 and targets[0].endswith("Box.size")   # not .inner


def test_question_kind():
    swe = _load("swebench")
    assert swe.question_kind("Box.size() crashes", ["pkg.mod.Box.size"]) == "named"
    assert swe.question_kind("the box is too big", ["pkg.mod.Box.size"]) == "unnamed"


def test_versus_counts_overlap_and_the_either_ceiling():
    report = _load("bench_report")
    rows = []
    for qid, a, b in [("t1", 1, 1), ("t2", 1, None), ("t3", None, 2), ("t4", None, None)]:
        rows += [{"question_id": qid, "arm": "A", "rank": a},
                 {"question_id": qid, "arm": "B", "rank": b}]
    k1 = report.versus(rows, "A", "B").splitlines()[2].split()
    assert k1 == ["1", "1", "1", "0", "2", "50%"]      # k both onlyA onlyB neither either
    k5 = report.versus(rows, "A", "B").splitlines()[3].split()
    assert k5 == ["5", "1", "1", "1", "1", "75%"]


def test_with_without_pairs_grep_files_against_codeorbit():
    swe = _load("swebench")
    rows = []
    for qid, files, cons, outl in [("t1", 10_000, 4_000, 1_000), ("t2", None, 50_000, 3_000)]:
        rows += [{"question_id": qid, "arm": "grep", "fn_targets": 1, "tok_files": files},
                 {"question_id": qid, "arm": "codeorbit", "fn_targets": 1,
                  "tok_symbols": outl, "tok_symbols_conservative": cons}]
    w = swe.with_without(rows)
    assert w["n"] == 2
    assert w["avg_without"] == (10_000 + swe.TOKEN_CAP) // 2    # not found costs the cap
    assert w["avg_with"] == 2_000
    assert w["without_within_32k"] == 0.5 and w["with_within_2k"] == 0.5
    assert w["fewer_tokens_with_conservative"] == 2


def test_rrf_weights_and_depths_limit_a_lists_vote():
    lists = [["x", "a"], ["a", "b"], ["a", "c"]]
    assert bench.rrf(lists, 3)[0] == "a"
    # A half-weight list cannot put its own top pick above a two-list consensus...
    assert bench.rrf([["x"], ["a"], ["a"]], 2, weights=[0.5, 1, 1])[0] == "a"
    # ...and with depth 1 the first list votes for nothing below its top.
    assert "a" not in bench.rrf([["x", "a"]], 5, depths=[1])


def test_fuse_with_zero_name_weight_is_hybrid():
    name, b, d = ["n1", "n2"], ["a", "b", "c"], ["b", "a", "d"]
    off = {"name_weight": 0.0, "name_depth": None, "only_coded": False}
    assert bench.fuse(name, b, d, "text", 5, off)[:4] == bench.rrf([b, d], 5)[:4]


def test_only_coded_drops_the_name_list_for_prose_issues():
    s = {"name_weight": 1.0, "name_depth": None, "only_coded": True}
    prose, coded = "the page loads slowly", "Session.resolve_redirects loops"
    assert bench.names_code(coded) and not bench.names_code(prose)
    assert bench.fuse(["n"], ["a"], ["a"], prose, 5, s) == ["a"]
    assert "n" in bench.fuse(["n"], ["a"], ["a"], coded, 5, s)


def test_tune_scores_saved_lists():
    tune = _load("tune_fusion")
    tasks = [{"targets": {"a"}, "text": "",
              "lists": {"name": ["x"], "bm25": ["a", "x"], "dense": ["a"]}}]
    off = {"name_weight": 0.0, "name_depth": None, "only_coded": False}
    assert tune.score(tasks, off) == {"R@1": 1.0, "R@5": 1.0, "MRR": 1.0}


def test_contains_target_needs_the_whole_name():
    assert bench.contains_target("### class pkg.mod.Utils\n", "pkg.mod.Utils")
    assert not bench.contains_target("### method pkg.mod.Utils.slugify\n", "pkg.mod.Utils")
    assert not bench.contains_target("see xpkg.mod.Utils here", "pkg.mod.Utils")
