"""Language dispatch for extraction.

Three tiers, tried in that order:

  1. A hand-written extractor - Python, JavaScript, TypeScript. These are the
     languages the tool is measured on, and a tuned extractor gets details a
     generic one cannot: PEP 257 docstrings, an arrow function bound to a const,
     `extends B implements C`.
  2. The generic tree-sitter extractor, driven by a per-language table and,
     failing that, by node types read out of the grammar itself. This is what
     makes the other ~360 languages work.
  3. The embedded extractor, for files whose code lives inside a script block:
     `.vue`, `.svelte`, `.astro`, `.html`.

Parsers are built on demand and cached. That is not a micro-optimisation: the
old module constructed four `Language` and four `Parser` objects at import time,
and with a registry of hundreds that would be seconds of startup for a command
that parses one file - or none, in the case of `codeorbit mcp`.
"""
from __future__ import annotations

import functools
from pathlib import Path

from .. import langs
from . import embedded, generic, javascript_ex, python_ex, typescript_ex
from .base import CallSite, Extraction, ImportStmt, Symbol

_TUNED_EXTRACTORS = {
    "python": python_ex.extract,
    "javascript": javascript_ex.extract,
    "typescript": typescript_ex.extract,
    "tsx": typescript_ex.extract,
}


class UnsupportedLanguage(Exception):
    """No grammar on this machine can read this file."""


@functools.lru_cache(maxsize=512)
def spec_for(lang: str) -> generic.Spec | None:
    """The generic extractor's table for a language, inferred if untabled."""
    tuned = generic.TUNED.get(lang)
    if tuned is not None:
        return tuned
    grammar = langs.grammar(lang)
    if grammar is None:
        return None
    return generic.infer_spec(grammar)


def module_qname(rel_path: str) -> str:
    """Repo-relative path -> dotted module name used as the root scope."""
    p = Path(rel_path)
    parts = list(p.parts[:-1]) + [p.stem]
    if parts and parts[-1] in ("__init__", "index", "mod"):
        parts = parts[:-1] or [p.stem]
    return ".".join(parts) if parts else p.stem


def parse_and_extract(source: bytes, lang: str, rel_path: str) -> Extraction:
    qname = module_qname(rel_path)

    if lang in langs.EMBEDDED:
        return _embedded(source, lang, qname)

    parser = langs.parser(lang)
    if parser is None:
        raise UnsupportedLanguage(lang)

    tree = parser.parse(source)

    tuned = _TUNED_EXTRACTORS.get(lang)
    if tuned is not None:
        return tuned(tree, source, qname)

    spec = spec_for(lang)
    if spec is None:
        raise UnsupportedLanguage(lang)
    return generic.extract(tree, source, qname, lang, spec)


def _embedded(source: bytes, lang: str, qname: str) -> Extraction:
    """A single-file component: parse its script block as JS or TS."""
    prepared = embedded.prepare(source)
    if prepared is None:
        # Markup with no script. A real module with no symbols, not a failure -
        # the module node still belongs in the graph so the file shows up.
        return Extraction(symbols=[Symbol(
            name=qname.rsplit(".", 1)[-1], qname=qname, kind="module",
            start_line=1, end_line=source.count(b"\n") + 1,
        )])

    script, inner = prepared
    parser = langs.parser(inner)
    if parser is None:
        raise UnsupportedLanguage(inner)
    tree = parser.parse(script)
    return _TUNED_EXTRACTORS[inner](tree, script, qname)


__all__ = [
    "parse_and_extract", "module_qname", "spec_for", "UnsupportedLanguage",
    "Extraction", "Symbol", "CallSite", "ImportStmt",
]
