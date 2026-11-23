"""Embedding build: batching, the reuse cache, and search - with a fake model."""
from pathlib import Path

from codeorbit import db, llm, semantic
from codeorbit.indexer import index_project
from codeorbit.resolve import resolve_project

SOURCE = '''
def parse_header(value):
    """Split a header into its parts."""
    return value.split(";")


def follow_redirects(response):
    return response.next


def merge_cookies(jar, other):
    jar.update(other)
    return jar
'''


def _fake_embedder(monkeypatch):
    """Deterministic vectors from the text; records every text sent."""
    sent: list[str] = []

    def embed(texts, model=llm.EMBED_MODEL):
        sent.extend(texts)
        return [[float((hash(t) >> s) % 97 + 1) for s in range(0, 64, 8)] for t in texts]

    monkeypatch.setattr(llm, "embed", embed)
    return sent


def _project(tmp_path: Path, source: str) -> Path:
    (tmp_path / "mod.py").write_text(source, encoding="utf-8")
    index_project(tmp_path)
    resolve_project(tmp_path)
    return tmp_path


def test_every_symbol_is_embedded_once(tmp_path, monkeypatch):
    sent = _fake_embedder(monkeypatch)
    root = _project(tmp_path, SOURCE)
    conn = db.connect(root)
    st = semantic.build(conn, root)
    assert st["embedded"] == st["total"] == 3 and st["reused"] == 0
    assert len(sent) == 3
    assert semantic.build(conn, root)["embedded"] == 0      # nothing pending
    conn.close()


def test_reindexing_reuses_vectors_of_unchanged_functions(tmp_path, monkeypatch):
    sent = _fake_embedder(monkeypatch)
    root = _project(tmp_path, SOURCE)
    conn = db.connect(root)
    semantic.build(conn, root)
    conn.close()
    sent.clear()

    # Edit one function. Re-indexing drops every node in the file - and with
    # them all three vectors - but only the edited function is new text.
    _project(tmp_path, SOURCE.replace("return response.next", "return response.location"))
    conn = db.connect(root)
    st = semantic.build(conn, root)
    conn.close()
    assert st["total"] == 3
    assert st["reused"] == 2
    assert len(sent) == 1 and "response.location" in sent[0]


def test_reused_vectors_search_the_same_as_fresh_ones(tmp_path, monkeypatch):
    _fake_embedder(monkeypatch)
    root = _project(tmp_path, SOURCE)
    conn = db.connect(root)
    semantic.build(conn, root)
    before = semantic.search(conn, "split header", limit=3)
    conn.close()

    _project(tmp_path, SOURCE + "\n\nX = 1\n")      # file changes, functions do not
    conn = db.connect(root)
    assert semantic.build(conn, root)["reused"] == 3
    after = semantic.search(conn, "split header", limit=3)
    conn.close()
    assert [s for s, _ in before] == [s for s, _ in after]   # same vectors, same scores
    assert len(after) == 3


def _older_version_index(tmp_path, monkeypatch):
    """Embeddings present, cache empty - as left by a version before the cache."""
    sent = _fake_embedder(monkeypatch)
    root = _project(tmp_path, SOURCE)
    conn = db.connect(root)
    semantic.build(conn, root)
    conn.execute("DELETE FROM embed_cache")
    conn.commit()
    conn.close()
    sent.clear()
    return root, sent


def test_an_older_index_seeds_the_cache_on_the_next_build(tmp_path, monkeypatch):
    root, sent = _older_version_index(tmp_path, monkeypatch)
    conn = db.connect(root)
    semantic.build(conn, root)                       # nothing pending: only seeds
    conn.close()

    _project(tmp_path, SOURCE + "\n\nX = 1\n")      # then an edit and a re-index
    conn = db.connect(root)
    st = semantic.build(conn, root)
    conn.close()
    assert st["reused"] == 3 and sent == []


def test_seeding_skips_a_file_edited_since_it_was_indexed(tmp_path, monkeypatch):
    """Its text on disk is no longer what was embedded: caching it would pair
    an old vector with new text."""
    root, sent = _older_version_index(tmp_path, monkeypatch)
    # Same line count, different body, index not updated.
    (root / "mod.py").write_text(SOURCE.replace("value.split", "value.rsplit"),
                                 encoding="utf-8")
    conn = db.connect(root)
    semantic.build(conn, root)
    cached = conn.execute("SELECT COUNT(*) FROM embed_cache").fetchone()[0]
    conn.close()
    assert cached == 0

    _project(tmp_path, SOURCE.replace("value.split", "value.rsplit"))
    conn = db.connect(root)
    st = semantic.build(conn, root)
    conn.close()
    assert st["reused"] == 0 and len(sent) == 3     # embedded fresh, not mismatched
