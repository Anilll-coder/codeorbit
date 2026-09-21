"""Single-file components: pull the script out and parse that.

A `.vue`, `.svelte` or `.astro` file is not JavaScript, so a JavaScript parser
chokes on it, and its own grammar knows the markup but exposes no functions.
Both answers are useless: these files are where the logic of a modern frontend
actually lives, and leaving them out meant a Vue or Svelte project indexed to a
handful of plain `.js` utilities and nothing else.

So the wrapper is stripped and the script handed to the JavaScript or TypeScript
extractor, which already knows how to read it.

The one thing that must not be lost is line numbers - every symbol the graph
stores is cited as `file:line`, and an offset citation is worse than none
because the reader follows it and lands on the wrong code. Rather than slicing
the script out and adding an offset back afterwards, everything *outside* the
script is overwritten with blank lines of exactly the same shape. The parser
then sees a file that is the right length with the code in the right place, and
every line number it reports is already correct.
"""
from __future__ import annotations

import re

# <script>, <script setup lang="ts">, <script context="module">
SCRIPT = re.compile(
    rb"<script(?P<attrs>[^>]*)>(?P<body>.*?)</script\s*>",
    re.DOTALL | re.IGNORECASE,
)
# Astro frontmatter: a --- fenced TypeScript block at the very top of the file.
FRONTMATTER = re.compile(rb"\A(?P<fence>---\r?\n)(?P<body>.*?)(?P<end>\r?\n---)",
                         re.DOTALL)

LANG_ATTR = re.compile(rb"""lang\s*=\s*["']?(?P<lang>[\w-]+)""", re.IGNORECASE)

TS_LANGS = {b"ts", b"typescript", b"tsx"}


def _blank(chunk: bytes) -> bytes:
    """Same length, same line count, no code: newlines kept, rest spaced out."""
    return bytes(c if c in (10, 13) else 32 for c in chunk)


def script_regions(src: bytes) -> list[tuple[int, int, bool]]:
    """[(start, end, is_typescript)] for every script block in the file."""
    out: list[tuple[int, int, bool]] = []

    fm = FRONTMATTER.match(src)
    if fm:
        out.append((fm.start("body"), fm.end("body"), True))

    for m in SCRIPT.finditer(src):
        attr = LANG_ATTR.search(m.group("attrs") or b"")
        is_ts = bool(attr and attr.group("lang").lower() in TS_LANGS)
        out.append((m.start("body"), m.end("body"), is_ts))
    return out


def prepare(src: bytes) -> tuple[bytes, str] | None:
    """Return (source with only the script left, inner language), or None.

    None means there is no script here at all, which is a real and common case -
    a presentational `.vue` or a static `.html` - and the caller should not
    bother the parser with it.
    """
    regions = script_regions(src)
    if not regions:
        return None

    keep = bytearray(_blank(src))
    any_ts = False
    for start, end, is_ts in regions:
        keep[start:end] = src[start:end]
        any_ts = any_ts or is_ts

    return bytes(keep), ("typescript" if any_ts else "javascript")
