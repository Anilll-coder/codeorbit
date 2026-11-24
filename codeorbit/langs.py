"""Which files CodeOrbit can read, and which grammar reads each one.

The old answer was a nine-entry dict: Python, JavaScript, TypeScript. Every
other repository - a Go service, a Rails app, a Spring backend, anything with a
`.vue` in it - indexed to nothing and the tool had no way to say why. That is
not a missing feature, it is the tool being wrong about most codebases.

So the registry is the thing that decides, and it has three layers:

  1. `SUFFIX` / `FILENAME` map a file onto a language id.
  2. `grammar(lang)` finds a tree-sitter parser for that id - from the three
     grammars CodeOrbit depends on directly, or from tree-sitter-language-pack,
     which ships ~370 more as prebuilt wheels.
  3. `EMBEDDED` handles the file types whose real code lives inside a script
     block (`.vue`, `.svelte`, `.astro`, `.html`): the wrapper is sliced off and
     the contents handed to the JavaScript or TypeScript grammar.

A language id is deliberately the *grammar* name, not a display name, so adding
a language is one line here and nothing else. `FAMILY` then groups ids that can
genuinely call each other, which is what keeps name resolution from binding a
Go `Load` to a Python `load`.

Nothing here fails hard when the language pack is absent. The three built-in
grammars keep working and `supported()` simply returns a shorter list, because a
missing optional dependency should narrow the tool, not break it.
"""
from __future__ import annotations

import functools

# --------------------------------------------------------------- suffixes

# suffix -> language id. Ids are tree-sitter grammar names.
#
# Only languages whose extraction is tested are listed - each has a test in
# tests/test_polyglot.py (or its own test file) checking that classes, methods,
# functions and calls come out right. A language a grammar can parse but whose
# symbols were never checked is left out: indexing it to wrong names is worse
# than skipping it, because a wrong graph looks exactly like a right one.
SUFFIX: dict[str, str] = {
    # ---- hand-written extractors
    ".py": "python", ".pyi": "python", ".pyw": "python",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript", ".mts": "typescript", ".cts": "typescript",
    ".tsx": "tsx",

    # ---- systems
    ".go": "go",
    ".rs": "rust",
    ".c": "c", ".h": "c",           # a C++ .h is detected and read as C++
    ".cc": "cpp", ".cpp": "cpp", ".cxx": "cpp", ".c++": "cpp",
    ".hh": "cpp", ".hpp": "cpp", ".hxx": "cpp", ".h++": "cpp", ".inl": "cpp",

    # ---- JVM
    ".java": "java",
    ".kt": "kotlin", ".kts": "kotlin",
    ".scala": "scala", ".sc": "scala",

    # ---- scripting / dynamic
    ".rb": "ruby", ".rake": "ruby", ".gemspec": "ruby", ".ru": "ruby",
    ".php": "php", ".php4": "php", ".php5": "php", ".phtml": "php",
    ".dart": "dart",
    ".swift": "swift",

    # ---- components whose script block is JavaScript or TypeScript
    ".vue": "vue",
    ".svelte": "svelte",
    ".astro": "astro",
    ".html": "html", ".htm": "html",
}

# Files whose name, not suffix, says what they are.
FILENAME: dict[str, str] = {
    "Rakefile": "ruby", "Gemfile": "ruby", "Vagrantfile": "ruby",
    "Brewfile": "ruby", "Podfile": "ruby", "Fastfile": "ruby",
    "SConstruct": "python", "SConscript": "python",
}

# Files that are a wrapper around a script block. The wrapper grammar sees no
# functions, so slice the script out and hand it to a grammar that does.
# name -> (outer language, default inner language)
EMBEDDED: dict[str, str] = {
    "vue": "javascript",
    "svelte": "javascript",
    "astro": "typescript",
    "html": "javascript",
}

# The three grammars CodeOrbit depends on directly, so the core languages work
# whether or not the language pack is installed.
BUILTIN = ("python", "javascript", "typescript", "tsx")

# Ids that can genuinely call each other. Resolution is scoped to a family: a
# name match across a family boundary is a coincidence, not a call. Anything
# not listed here is its own family, which is the safe default.
FAMILY: dict[str, str] = {
    "javascript": "js", "typescript": "js", "tsx": "js", "vue": "js",
    "svelte": "js", "astro": "js", "html": "js",
    "python": "py",
    "c": "c", "cpp": "c",
    "php": "php",
    "ruby": "rb",
}


def family(lang: str) -> str:
    """The resolution family a language belongs to."""
    return FAMILY.get(lang, lang)


# ---------------------------------------------------------------- lookup

def lang_for(name: str) -> str | None:
    """Language id for a file name, or None if CodeOrbit does not read it.

    Double-barrelled suffixes are tried first so `page.blade.php` is Blade and
    not PHP, and `a.d.ts` is caught by the caller's own filter rather than
    silently parsed as a TypeScript module full of declarations.
    """
    hit = FILENAME.get(name)
    if hit:
        return hit

    lowered = name.lower()
    parts = lowered.split(".")
    # Longest first, so `page.blade.php` is Blade rather than PHP.
    for i in range(min(len(parts) - 1, 2), 0, -1):
        hit = SUFFIX.get("." + ".".join(parts[-i:]))
        if hit:
            return hit
    return None


@functools.lru_cache(maxsize=1)
def _pack():
    """tree-sitter-language-pack, or None when it is not installed."""
    try:
        import tree_sitter_language_pack as pack
    except Exception:      # noqa: BLE001 - absence is a supported state
        return None
    return pack


@functools.lru_cache(maxsize=1)
def _pack_names() -> frozenset[str]:
    pack = _pack()
    if pack is None:
        return frozenset()
    try:
        import typing
        return frozenset(typing.get_args(pack.SupportedLanguage))
    except Exception:      # noqa: BLE001
        return frozenset()


@functools.lru_cache(maxsize=512)
def grammar(lang: str):
    """A tree_sitter.Language for `lang`, or None if nothing can parse it.

    Cached because building one is not free and the indexer asks per file.
    """
    from tree_sitter import Language

    if lang == "python":
        import tree_sitter_python as ts
        return Language(ts.language())
    if lang == "javascript":
        import tree_sitter_javascript as ts
        return Language(ts.language())
    if lang in ("typescript", "tsx"):
        import tree_sitter_typescript as ts
        # TSX is a separate grammar, not a flag: .tsx needs it or JSX fails to
        # parse, and .ts needs the plain one or a type assertion reads as JSX.
        return Language(ts.language_tsx() if lang == "tsx"
                        else ts.language_typescript())

    pack = _pack()
    if pack is None:
        return None
    try:
        return pack.get_language(lang)
    except Exception:      # noqa: BLE001 - a grammar that will not load
        return None


@functools.lru_cache(maxsize=512)
def parser(lang: str):
    """A ready tree_sitter.Parser for `lang`, or None."""
    from tree_sitter import Parser

    g = grammar(lang)
    return Parser(g) if g is not None else None


@functools.lru_cache(maxsize=1)
def supported() -> frozenset[str]:
    """Language ids this installation can actually parse right now.

    Computed from the registry rather than declared, so it cannot drift: a
    suffix mapped to a grammar nothing provides is simply not supported, and
    `scan` will skip those files instead of indexing them to nothing.
    """
    wanted = set(SUFFIX.values()) | set(FILENAME.values()) | set(BUILTIN)
    names = _pack_names()
    return frozenset(
        lang for lang in wanted
        if lang in BUILTIN or lang in names
    )


def is_supported(lang: str) -> bool:
    return lang in supported()


def summary() -> dict:
    """What `codeorbit langs` prints: coverage, and why it is what it is."""
    langs = supported()
    suffixes = sorted({s for s, l in SUFFIX.items() if l in langs})
    return {
        "pack": _pack() is not None,
        "languages": sorted(langs),
        "count": len(langs),
        "suffixes": suffixes,
        "embedded": sorted(EMBEDDED),
    }
