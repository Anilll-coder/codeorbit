"""Ranking symbols for a question: the fusion the benchmark measured best.

Three lists, one reciprocal rank fusion:

  * name search - `context.keyword_ranking`, which wins when the question
    names the code (`Session.send`, `parse_http_date`);
  * BM25 over each symbol's name, signature, docstring and body - wins on the
    words an issue shares with the code it is about;
  * dense embeddings - wins when the question describes behaviour in words the
    code never uses.

On SWE-bench Lite's held-out tasks this ordering beat BM25 + dense alone and
beat the name search alone (README, "On real GitHub issues"). It lived in
scripts/benchmark.py while it was being measured; it is here so the tools an
agent calls rank exactly the way the benchmark scored.
"""
from __future__ import annotations

import hashlib
import math
import re
from pathlib import Path

from . import context as ctxmod
from . import query


class Sources:
    """File lines, read once. Symbol bodies are sliced from here."""

    def __init__(self, root: Path):
        self.root = root
        self._lines: dict[str, list[str]] = {}

    def body(self, row) -> str:
        lines = self._lines.get(row["path"])
        if lines is None:
            try:
                lines = (self.root / row["path"]).read_text(
                    encoding="utf-8", errors="replace").splitlines()
            except OSError:
                lines = []
            self._lines[row["path"]] = lines
        return "\n".join(lines[row["start_line"] - 1:row["end_line"]])


_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_CAMEL = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|[0-9]+")


def code_tokens(text: str) -> list[str]:
    """Identifiers, plus their snake_case / camelCase parts, lower-cased."""
    out = []
    for ident in _IDENT.findall(text):
        low = ident.lower()
        out.append(low)
        parts = [p.lower() for piece in ident.split("_") for p in _CAMEL.findall(piece)]
        if len(parts) > 1:
            out.extend(p for p in parts if len(p) > 1)
    return out


class BM25:
    """Okapi BM25 over function/class/method chunks.

    Module nodes are left out: a whole file is a chunk no code-RAG would use,
    and it would win every query on term frequency alone.
    """

    def __init__(self, conn, src: Sources, k1: float = 1.2, b: float = 0.75):
        self.k1, self.b = k1, b
        self.ids: list[int] = []
        self.lengths: list[int] = []
        self.postings: dict[str, list[tuple[int, int]]] = {}
        rows = conn.execute(
            f"SELECT {query.NODE_COLS} FROM nodes n JOIN files f ON f.id = n.file_id "
            "WHERE n.kind != 'module'").fetchall()
        for row in rows:
            doc = " ".join(filter(None, [
                row["kind"], row["name"], row["qname"], row["signature"],
                row["docstring"], src.body(row)]))
            toks = code_tokens(doc)
            tf: dict[str, int] = {}
            for t in toks:
                tf[t] = tf.get(t, 0) + 1
            d = len(self.ids)
            self.ids.append(row["id"])
            self.lengths.append(len(toks))
            for t, n in tf.items():
                self.postings.setdefault(t, []).append((d, n))
        self.n = len(self.ids)
        self.avgdl = (sum(self.lengths) / self.n) if self.n else 1.0

    def search(self, question: str, limit: int) -> list[int]:
        terms = {t for t in code_tokens(question) if t not in ctxmod.STOP}
        scores: dict[int, float] = {}
        for t in terms:
            post = self.postings.get(t)
            if not post:
                continue
            idf = math.log(1 + (self.n - len(post) + 0.5) / (len(post) + 0.5))
            for d, tf in post:
                norm = tf + self.k1 * (1 - self.b + self.b * self.lengths[d] / self.avgdl)
                scores[d] = scores.get(d, 0.0) + idf * tf * (self.k1 + 1) / norm
        best = sorted(scores.items(), key=lambda p: -p[1])[:limit]
        return [self.ids[d] for d, _ in best]


def rrf(lists: list[list], limit: int, weights: list[float] | None = None,
        depths: list[int | None] | None = None) -> list:
    """Reciprocal rank fusion. A list's vote is scaled by its weight and stops
    after its depth, so a noisy list can break ties without outvoting the rest."""
    fused: dict = {}
    for i, ids in enumerate(lists):
        w = weights[i] if weights else 1.0
        d = depths[i] if depths and depths[i] else len(ids)
        for rank, nid in enumerate(ids[:d]):
            fused[nid] = fused.get(nid, 0.0) + w / (ctxmod.RRF_K + rank + 1)
    return [nid for nid, _ in sorted(fused.items(), key=lambda p: -p[1])[:limit]]


# The fusion arm's name-search vote. Equal weight, full depth is where it
# started; scripts/tune_fusion.py measures alternatives on the development
# tasks from saved lists, and the chosen setting is frozen here (benchmark.py
# re-exports it as bench.FUSION).
FUSION = {"name_weight": 1.0, "name_depth": None, "only_coded": False}

_CODED = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+"
                    r"|\b[a-z]+_[a-z_0-9]+\b|\b[a-z]+[A-Z][A-Za-z0-9]*\b")


def names_code(text: str) -> bool:
    """Does the text name something the way code does (a.b, snake_case, camelCase)?"""
    return bool(_CODED.search(text))


def fuse(name: list, bm25_ids: list, dense: list, text: str, limit: int,
         settings: dict = FUSION) -> list:
    use_name = not settings["only_coded"] or names_code(text)
    lists = ([name] if use_name else []) + [bm25_ids, dense]
    weights = ([settings["name_weight"]] if use_name else []) + [1.0, 1.0]
    depths = ([settings["name_depth"]] if use_name else []) + [None, None]
    return rrf(lists, limit, weights, depths)


def dense_ids(conn, question: str, limit: int) -> list[int]:
    from codeorbit import semantic as sem
    return [nid for _, nid in sem.search(conn, question, limit=limit)]




# BM25 reads every symbol's body, which is seconds on a large repository, so
# one index is kept per graph and rebuilt only when the graph's files change.
_BM25_CACHE: dict[str, tuple[str, BM25]] = {}


def _graph_signature(conn) -> str:
    h = hashlib.sha1()
    for row in conn.execute("SELECT path, hash FROM files ORDER BY path"):
        h.update(f"{row['path']}\0{row['hash']}\n".encode())
    return h.hexdigest()


def bm25_for(root: Path, conn) -> BM25:
    key = str(Path(root).resolve())
    sig = _graph_signature(conn)
    hit = _BM25_CACHE.get(key)
    if hit is None or hit[0] != sig:
        bm = _load_bm25(Path(root), sig)
        if bm is None:
            bm = BM25(conn, Sources(Path(root)))
            _save_bm25(Path(root), sig, bm)
        hit = (sig, bm)
        _BM25_CACHE[key] = hit
    return hit[1]


# Also kept on disk, beside the graph: a process that starts per request (the
# Claude Code hook) would otherwise rebuild it every time - 8 s on Django. The
# file is named by the graph's signature, so a changed graph never reads a
# stale index; it is written by this code into the project's own .codeorbit/.
def _bm25_path(root: Path, sig: str) -> Path:
    from .db import db_path
    return db_path(root).parent / f"bm25-{sig[:16]}.pickle"


def _load_bm25(root: Path, sig: str) -> BM25 | None:
    import pickle
    try:
        with open(_bm25_path(root, sig), "rb") as f:
            bm = pickle.load(f)
    except (OSError, pickle.PickleError, EOFError, AttributeError):
        return None
    return bm if isinstance(bm, BM25) else None


def _save_bm25(root: Path, sig: str, bm: BM25) -> None:
    import pickle
    path = _bm25_path(root, sig)
    try:
        for old in path.parent.glob("bm25-*.pickle"):
            old.unlink()
        tmp = path.with_suffix(".tmp")
        with open(tmp, "wb") as f:
            pickle.dump(bm, f, protocol=pickle.HIGHEST_PROTOCOL)
        tmp.replace(path)
    except OSError:
        pass


def fusion_ranking(root: Path, conn, question: str, limit: int = 50) -> list[int]:
    """Node ids for a question, best first: name search + BM25 + dense, fused.

    Embeddings are optional: without them the dense vote is simply absent.
    """
    pool = max(limit, 50)       # the depth every list was measured at
    names = ctxmod.keyword_ranking(conn, question, pool)
    b_ids = bm25_for(root, conn).search(question, pool)
    try:
        from . import semantic as sem
        d_ids = dense_ids(conn, question, pool) if sem.available(conn) else []
    except Exception:           # noqa: BLE001 - embeddings never fail a query
        d_ids = []
    return fuse(names, b_ids, d_ids, question, pool)[:limit]
