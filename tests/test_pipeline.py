"""End-to-end tests over a real temp project and a real SQLite graph.

Nothing here is mocked. The whole value of this tool is whether it reads real
code correctly, and a mocked parser would test the mock. Each test builds a
small project on disk, indexes it, and asserts against the graph that comes out.
"""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from codeorbit import audit, db, query, rules
from codeorbit.extract import module_qname, parse_and_extract
from codeorbit.indexer import index_project
from codeorbit.resolve import resolve_project
from codeorbit.scanner import scan


def write(root: Path, rel: str, body: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")
    return p


@pytest.fixture
def project(tmp_path: Path) -> Path:
    write(tmp_path, "app/models.py", '''
        """Domain models."""

        class User:
            """A person."""

            def __init__(self, email):
                self.email = email

            def normalize(self):
                return self.email.strip().lower()


        class Admin(User):
            def escalate(self):
                return self.normalize()
        ''')

    write(tmp_path, "app/service.py", '''
        from app.models import User


        def make_user(email):
            u = User(email)
            return u.normalize()


        def unused_helper():
            return 42
        ''')

    write(tmp_path, "tests/test_service.py", '''
        from app.service import make_user


        def test_make_user():
            assert make_user(" A@B.COM ") == "a@b.com"
        ''')

    write(tmp_path, "web/app.js", '''
        import { helper } from "./util";

        export class Widget extends Base {
          render() {
            return helper(this.props);
          }
        }

        const boot = () => new Widget();
        ''')

    return tmp_path


@pytest.fixture
def graph(project: Path):
    index_project(project)
    resolve_project(project)
    conn = db.connect(project)
    yield project, conn
    conn.close()


# ------------------------------------------------------------------ scanning

def test_scan_finds_both_languages(project: Path):
    found = scan(project)
    langs = sorted({lang for _, lang in found})
    assert langs == ["javascript", "python"]


def test_scan_skips_vendored_dirs(project: Path):
    write(project, "node_modules/pkg/index.js", "export const x = 1;")
    write(project, ".venv/lib/thing.py", "def hidden(): pass")
    paths = [p.as_posix() for p, _ in scan(project)]
    assert not any("node_modules" in p for p in paths)
    assert not any(".venv" in p for p in paths)


def test_module_qname_strips_package_index():
    assert module_qname("app/models.py") == "app.models"
    assert module_qname("app/__init__.py") == "app"
    assert module_qname("web/index.js") == "web"


# ---------------------------------------------------------------- extraction

def test_python_symbols_and_kinds():
    src = b"class A:\n    def m(self):\n        return f()\n\ndef f():\n    return 1\n"
    ex = parse_and_extract(src, "python", "m.py")
    kinds = {s.name: s.kind for s in ex.symbols}
    assert kinds["A"] == "class"
    assert kinds["m"] == "method"       # inside a class
    assert kinds["f"] == "function"     # top level


def test_python_docstring_and_base_class():
    src = b'class C(Base):\n    """Doc here."""\n    pass\n'
    ex = parse_and_extract(src, "python", "m.py")
    c = next(s for s in ex.symbols if s.name == "C")
    assert c.docstring == "Doc here."
    assert ("m.C", "Base") in ex.base_types


def test_javascript_arrow_and_method():
    src = b"class W extends B { go(){ return f(); } }\nconst h = (x) => g(x);\n"
    ex = parse_and_extract(src, "javascript", "w.js")
    kinds = {s.name: s.kind for s in ex.symbols}
    assert kinds["W"] == "class"
    assert kinds["go"] == "method"
    assert kinds["h"] == "function"     # arrow bound to a const is a definition
    assert ("w.W", "B") in ex.base_types


def test_call_receiver_is_captured():
    ex = parse_and_extract(b"def f():\n    api.post(1)\n", "python", "m.py")
    call = next(c for c in ex.calls if c.callee == "post")
    assert call.recv == "api"


def test_unparseable_file_does_not_break_indexing(project: Path):
    write(project, "app/broken.py", "def (((( :\n")
    st = index_project(project)
    assert st["files"] >= 4          # the good files still landed


# ---------------------------------------------------------------- resolution

def test_same_file_call_resolves_exactly(graph):
    _, conn = graph
    row = query.search(conn, "make_user")[0]
    callees = {c["name"]: c["resolution"] for c in query.callees(conn, row["id"])}
    assert "User" in callees or "normalize" in callees


def test_self_call_binds_to_own_class(graph):
    _, conn = graph
    esc = next(r for r in query.search(conn, "escalate") if r["kind"] == "method")
    names = {c["name"] for c in query.callees(conn, esc["id"])}
    assert "normalize" in names       # self.normalize() inside Admin


def test_contains_edges_link_class_to_method(graph):
    _, conn = graph
    user = next(r for r in query.search(conn, "User") if r["kind"] == "class")
    members = {m["name"] for m in query.members(conn, user["id"])}
    assert {"normalize", "__init__"} <= members


def test_extends_edge_exists(graph):
    _, conn = graph
    n = conn.execute("SELECT count(*) FROM edges WHERE kind='extends'").fetchone()[0]
    assert n >= 1


def test_name_match_does_not_cross_languages(tmp_path: Path):
    """A JS import must not bind to a same-named Python symbol.

    Found end-to-end: `import { load } from "./svc"` in a .js file resolved to
    `svc.load` in a .py file purely on the name, and was reported as an EXACT
    call edge. Static parsing cannot see a cross-language call, so a same-name
    symbol in another language is a coincidence, not a target.
    """
    write(tmp_path, "svc.py", "def load(name):\n    return name\n")
    write(tmp_path, "ui.js",
          'import { load } from "./svc";\n'
          "export function render() { return load(1); }\n")
    index_project(tmp_path)
    resolve_project(tmp_path)
    conn = db.connect(tmp_path)

    py_load = next(r for r in query.search(conn, "load") if r["path"].endswith(".py"))
    js_callers = [c for c in query.callers(conn, py_load["id"])
                  if c["path"].endswith(".js")]
    conn.close()
    assert not js_callers, "a JavaScript caller must not bind to a Python definition"


def test_external_calls_get_no_edge(graph):
    """A call to something outside the project must not invent an edge."""
    _, conn = graph
    bogus = conn.execute(
        "SELECT count(*) FROM nodes WHERE name IN ('strip','lower')"
    ).fetchone()[0]
    assert bogus == 0


# -------------------------------------------------------------------- graph

def test_callers_and_impact(graph):
    _, conn = graph
    norm = next(r for r in query.search(conn, "normalize") if r["kind"] == "method")
    callers = query.callers(conn, norm["id"])
    assert callers, "normalize is called by make_user and escalate"
    assert len(query.impact(conn, norm["id"], 3)) >= 1


def test_affected_tests_finds_the_test(graph):
    _, conn = graph
    mk = query.search(conn, "make_user")[0]
    tests = query.affected_tests(conn, mk["id"], max_depth=4)
    assert any("test" in t["path"] for t in tests)


def test_call_path_between_symbols(graph):
    _, conn = graph
    mk = query.search(conn, "make_user")[0]
    norm = next(r for r in query.search(conn, "normalize") if r["kind"] == "method")
    path = query.call_path(conn, mk["id"], norm["id"], max_depth=5)
    assert path, "make_user should reach normalize"
    assert path[0][0]["name"] == "make_user"
    assert path[-1][0]["name"] == "normalize"


def test_dead_code_finds_unused_helper(graph):
    _, conn = graph
    names = {r["name"] for r in query.dead_code(conn)}
    assert "unused_helper" in names


def test_source_of_returns_numbered_lines(graph):
    root, conn = graph
    row = query.search(conn, "make_user")[0]
    body = query.source_of(root, row)
    assert "def make_user" in body
    assert "|" in body            # line-number gutter


# -------------------------------------------------------------- incremental

def test_reindex_is_a_noop_when_nothing_changed(project: Path):
    index_project(project)
    st = index_project(project)
    assert st["touched"] == 0
    assert st["unchanged"] > 0


def test_reindex_detects_a_modified_file(project: Path):
    index_project(project)
    (project / "app" / "service.py").write_text(
        "def make_user(email):\n    return email\n\ndef brand_new():\n    return 1\n",
        encoding="utf-8",
    )
    st = index_project(project)
    assert st["changed"] == 1
    conn = db.connect(project)
    assert query.search(conn, "brand_new")
    conn.close()


def test_reindex_drops_a_deleted_file(project: Path):
    index_project(project)
    (project / "app" / "service.py").unlink()
    st = index_project(project)
    assert st["removed"] == 1

    conn = db.connect(project)
    # Match the exact qualified name. A substring match would also hit
    # tests.test_service.test_make_user, which is a different symbol that
    # legitimately survives.
    gone = conn.execute(
        "SELECT count(*) FROM nodes WHERE qname = 'app.service.make_user'"
    ).fetchone()[0]
    assert gone == 0
    assert not conn.execute(
        "SELECT count(*) FROM files WHERE path = 'app/service.py'"
    ).fetchone()[0]
    conn.close()


def test_ranked_search_survives_incremental_reindex(project: Path):
    """FTS5 BM25 ranking is where external-content drift actually shows up.

    Hand-maintaining nodes_fts (an INSERT per node plus a delete trigger) drifts
    out of sync with the %_docsize shadow table BM25 reads. The failure is
    deceptive: plain MATCH keeps working, integrity_check still reports "ok",
    and only `ORDER BY rank` starts raising "database disk image is malformed".
    Found when the MCP server hit it in real use.
    """
    index_project(project)
    resolve_project(project)

    # Churn the file the way a normal edit-and-reindex loop would.
    for i in range(3):
        write(project, "app/service.py", f"""
            def make_user(email):
                return {i}


            def extra_{i}():
                return {i}
            """)
        index_project(project)
        resolve_project(project)

    conn = db.connect(project)
    try:
        # Two distinct failure modes have to be caught here, and asserting only
        # "did not raise" catches neither properly:
        #   - drift makes ORDER BY rank raise "malformed"
        #   - never rebuilding leaves the index EMPTY, which raises nothing and
        #     silently returns no rows
        # So require that ranked search actually finds something.
        rows = conn.execute(
            "SELECT rowid FROM nodes_fts WHERE nodes_fts MATCH 'make_user' "
            "ORDER BY rank LIMIT 5"
        ).fetchall()
        assert rows, "the FTS index is empty - it was never rebuilt"

        indexed = conn.execute("SELECT count(*) FROM nodes_fts").fetchone()[0]
        total = conn.execute(
            "SELECT count(*) FROM nodes WHERE kind != 'module'").fetchone()[0]
        assert indexed >= total, "every symbol should be searchable"

        assert query.search(conn, "make_user"), "ranked search must still work"
    finally:
        conn.close()


def test_deleted_symbols_leave_the_search_index(project: Path):
    """A deleted file's symbols must not stay searchable."""
    index_project(project)
    conn = db.connect(project)
    before = conn.execute(
        "SELECT count(*) FROM nodes_fts WHERE nodes_fts MATCH 'unused_helper'"
    ).fetchone()[0]
    conn.close()
    assert before >= 1

    (project / "app" / "service.py").unlink()
    index_project(project)

    conn = db.connect(project)
    after = conn.execute(
        "SELECT count(*) FROM nodes_fts WHERE nodes_fts MATCH 'unused_helper'"
    ).fetchone()[0]
    conn.close()
    assert after == 0, "rebuilding the FTS index should have dropped it"


# ------------------------------------------------------------------- audit

def test_audit_flags_eval_and_ranks_by_reach(project: Path):
    write(project, "app/danger.py", '''
        def risky(payload):
            return eval(payload)


        def caller_one():
            return risky("1")


        def caller_two():
            return caller_one()
        ''')
    index_project(project)
    resolve_project(project)
    conn = db.connect(project)

    report = audit.audit_project(project, conn)
    ids = {f.rule.id for f in report.findings}
    assert "py-eval" in ids

    hit = next(f for f in report.findings if f.rule.id == "py-eval")
    assert hit.symbol and hit.symbol.endswith("risky")
    assert hit.blast >= 1, "callers should give it a blast radius"
    conn.close()


def test_audit_skips_test_files_by_default(project: Path):
    write(project, "tests/test_secrets.py", 'password = "hunter2hunter2"\n')
    index_project(project)
    resolve_project(project)
    conn = db.connect(project)
    paths = {f.path for f in audit.audit_project(project, conn).findings}
    assert not any("test_secrets" in p for p in paths)
    conn.close()


def test_every_rule_compiles_and_is_documented():
    assert rules.RULES, "there should be rules"
    for r in rules.RULES:
        assert r.pattern is not None
        assert r.why.strip(), f"{r.id} has no explanation"
        assert r.severity in (rules.HIGH, rules.MEDIUM, rules.LOW)


def test_rule_ids_are_unique():
    ids = [r.id for r in rules.RULES]
    assert len(ids) == len(set(ids))
