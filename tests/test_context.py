"""Context formatting: what the model is shown, and what it is not charged twice."""
from codeorbit.context import call_lines, format_outline, group_neighbours


def _edge(qname, line, resolution="exact", path="pkg/a.py"):
    return {"qname": qname, "path": path, "call_line": line, "resolution": resolution}


def test_a_neighbour_called_several_times_is_listed_once():
    rows = [_edge("a.f", 10), _edge("a.g", 12), _edge("a.f", 30), _edge("a.f", 10)]
    grouped = group_neighbours(rows, limit=8)
    assert [g["qname"] for g in grouped] == ["a.f", "a.g"]
    assert call_lines(grouped[0]) == "10, 30"


def test_the_limit_counts_distinct_neighbours():
    rows = [_edge(f"a.f{i}", i) for i in range(10)]
    assert len(group_neighbours(rows, limit=3)) == 3


def test_one_exact_call_makes_the_neighbour_exact():
    rows = [_edge("a.f", 1, "heuristic"), _edge("a.f", 2, "exact")]
    assert group_neighbours(rows, 8)[0]["resolution"] == "exact"


def test_outline_has_signatures_and_lines_but_no_code():
    rows = [
        {"name": "Box", "kind": "class", "signature": None, "start_line": 1, "end_line": 9},
        {"name": "size", "kind": "method", "signature": "size(self)",
         "start_line": 3, "end_line": 4},
        {"name": "run", "kind": "function", "signature": "(x,\n    y)",
         "start_line": 11, "end_line": 20},
    ]
    out = format_outline("pkg/a.py", rows).splitlines()
    assert out[0] == "pkg/a.py: 3 definition(s)"
    assert out[1].split() == ["1-9", "class", "Box"]
    assert out[2].split() == ["3-4", "method", "size(self)"]
    assert out[3].endswith("function run(x, y)")         # name added, whitespace folded
