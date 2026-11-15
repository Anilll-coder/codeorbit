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
