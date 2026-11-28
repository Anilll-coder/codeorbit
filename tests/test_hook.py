"""The Claude Code prompt hook, and the caches that make one process per request cheap."""
from __future__ import annotations

import io
import json
import textwrap
from pathlib import Path

import pytest

from codeorbit import freshness, hook, rank
from codeorbit.db import db_path
from codeorbit.indexer import index_project
from codeorbit.resolve import resolve_project


@pytest.fixture
def project(tmp_path) -> Path:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "proxy.py").write_text(textwrap.dedent('''
        def rebuild_proxy_auth(request):
            request.headers.pop("Proxy-Authorization", None)
            return request
    '''), encoding="utf-8")
    index_project(tmp_path)
    resolve_project(tmp_path)
    freshness.forget()
    return tmp_path


def run_hook(payload: dict) -> str:
    out = io.StringIO()
    hook.main(stdin=io.StringIO(json.dumps(payload)), stdout=out)
    return out.getvalue()


def test_it_adds_the_ranked_code_to_the_prompt(project):
    out = run_hook({"prompt": "proxy authorization header kept on redirect",
                    "cwd": str(project)})
    assert out.startswith("CodeOrbit (automatic")
    assert "pkg/proxy.py" in out and "rebuild_proxy_auth" in out


def test_it_stays_silent_without_an_index_or_a_prompt(tmp_path, project):
    assert run_hook({"prompt": "anything", "cwd": str(tmp_path / "elsewhere")}) == ""
    assert run_hook({"prompt": "", "cwd": str(project)}) == ""
    out = io.StringIO()
    hook.main(stdin=io.StringIO("not json"), stdout=out)        # never raises
    assert out.getvalue() == ""


def test_the_ranking_index_is_kept_on_disk(project):
    from codeorbit import db
    rank._BM25_CACHE.clear()
    conn = db.connect(project)
    try:
        rank.bm25_for(project, conn)
        assert list(db_path(project).parent.glob("bm25-*.pickle"))
        rank._BM25_CACHE.clear()                     # a new process
        assert rank.bm25_for(project, conn).n > 0    # loaded, not rebuilt
    finally:
        conn.close()


def test_a_new_process_trusts_the_saved_snapshot(project):
    f = project / "pkg" / "proxy.py"
    f.write_text(f.read_text(encoding="utf-8") + "\n\ndef added():\n    pass\n",
                 encoding="utf-8")
    assert freshness.refresh(project) == ["pkg/proxy.py"]
    freshness.forget()                               # a new process
    assert freshness.refresh(project) == []          # nothing re-checked as changed
