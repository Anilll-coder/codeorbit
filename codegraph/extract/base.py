"""Shared types every language extractor produces."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Symbol:
    name: str
    qname: str
    kind: str            # module|class|function|method|variable|import
    start_line: int
    end_line: int
    signature: str | None = None
    docstring: str | None = None
    parent: str | None = None   # qname of the enclosing symbol, for `contains`


@dataclass
class CallSite:
    caller_qname: str    # the enclosing definition; module qname if top level
    callee: str          # name as written
    recv: str | None     # receiver for a.b() calls
    line: int


@dataclass
class ImportStmt:
    module: str
    symbol: str | None
    alias: str | None
    line: int


@dataclass
class Extraction:
    symbols: list[Symbol] = field(default_factory=list)
    calls: list[CallSite] = field(default_factory=list)
    imports: list[ImportStmt] = field(default_factory=list)
    base_types: list[tuple[str, str]] = field(default_factory=list)  # (class_qname, base_name)


def text(node, src: bytes) -> str:
    return src[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def line_of(node) -> int:
    return node.start_point[0] + 1
