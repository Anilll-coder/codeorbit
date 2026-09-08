"""Language dispatch for extraction."""
from __future__ import annotations

from pathlib import Path

import tree_sitter_javascript as tsjs
import tree_sitter_python as tsp
from tree_sitter import Language, Parser

from . import javascript_ex, python_ex
from .base import CallSite, Extraction, ImportStmt, Symbol

_LANGS = {"python": Language(tsp.language()), "javascript": Language(tsjs.language())}
_PARSERS = {name: Parser(lang) for name, lang in _LANGS.items()}
_EXTRACTORS = {"python": python_ex.extract, "javascript": javascript_ex.extract}


def module_qname(rel_path: str) -> str:
    """Repo-relative path -> dotted module name used as the root scope."""
    p = Path(rel_path)
    parts = list(p.parts[:-1]) + [p.stem]
    if parts and parts[-1] in ("__init__", "index"):
        parts = parts[:-1] or [p.stem]
    return ".".join(parts) if parts else p.stem


def parse_and_extract(source: bytes, lang: str, rel_path: str) -> Extraction:
    tree = _PARSERS[lang].parse(source)
    return _EXTRACTORS[lang](tree, source, module_qname(rel_path))


__all__ = ["parse_and_extract", "module_qname", "Extraction", "Symbol", "CallSite", "ImportStmt"]
