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
SUFFIX: dict[str, str] = {
    # ---- the three CodeOrbit parses with its own grammars
    ".py": "python", ".pyi": "python", ".pyw": "python",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript", ".mts": "typescript", ".cts": "typescript",
    ".tsx": "tsx",

    # ---- systems
    ".go": "go",
    ".rs": "rust",
    ".c": "c", ".h": "c",
    ".cc": "cpp", ".cpp": "cpp", ".cxx": "cpp", ".c++": "cpp",
    ".hh": "cpp", ".hpp": "cpp", ".hxx": "cpp", ".h++": "cpp", ".inl": "cpp",
    ".m": "objc", ".mm": "objc",
    ".zig": "zig",
    ".v": "verilog", ".sv": "verilog", ".svh": "verilog",
    ".vhd": "vhdl", ".vhdl": "vhdl",
    ".nim": "nim", ".nims": "nim",
    ".d": "d",
    ".odin": "odin",
    ".cu": "cuda", ".cuh": "cuda",
    ".ada": "ada", ".adb": "ada", ".ads": "ada",
    ".f90": "fortran", ".f95": "fortran", ".f03": "fortran", ".f": "fortran",
    ".pas": "pascal", ".pp": "pascal",

    # ---- JVM / .NET
    ".java": "java",
    ".kt": "kotlin", ".kts": "kotlin",
    ".scala": "scala", ".sc": "scala",
    ".groovy": "groovy", ".gvy": "groovy",
    ".clj": "clojure", ".cljs": "clojure", ".cljc": "clojure", ".edn": "clojure",
    ".cs": "csharp", ".csx": "csharp",
    ".fs": "fsharp", ".fsx": "fsharp", ".fsi": "fsharp_signature",
    ".vb": "vb",

    # ---- scripting / dynamic
    ".rb": "ruby", ".rake": "ruby", ".gemspec": "ruby", ".ru": "ruby",
    ".php": "php", ".php4": "php", ".php5": "php", ".phtml": "php",
    ".pl": "perl", ".pm": "perl", ".t": "perl",
    ".lua": "lua",
    ".luau": "luau",
    ".tcl": "tcl",
    ".r": "r", ".R": "r",
    ".jl": "julia",
    ".dart": "dart",
    ".swift": "swift",
    ".ex": "elixir", ".exs": "elixir",
    ".erl": "erlang", ".hrl": "erlang",
    ".hs": "haskell", ".lhs": "haskell",
    ".elm": "elm",
    ".ml": "ocaml", ".mli": "ocaml_interface",
    ".re": "reason",
    ".res": "rescript",
    ".purs": "purescript",
    ".cr": "crystal",
    ".nix": "nix",
    ".rkt": "racket",
    ".scm": "scheme", ".ss": "scheme",
    ".lisp": "commonlisp", ".lsp": "commonlisp", ".cl": "commonlisp",
    ".el": "elisp",
    ".fnl": "fennel",
    ".janet": "janet",
    ".gleam": "gleam",
    ".gd": "gdscript",
    ".hx": "haxe",
    ".vala": "vala",
    ".sml": "sml",
    ".eiffel": "eiffel", ".e": "eiffel",
    ".mojo": "mojo", ".🔥": "mojo",
    ".pyx": "cython", ".pxd": "cython",
    ".as": "actionscript",
    ".apex": "apex", ".cls": "apex", ".trigger": "apex",
    ".sol": "solidity",
    ".move": "move",
    ".cairo": "cairo",
    ".pro": "prolog",
    ".matlab": "matlab",
    ".sas": "sas",
    ".awk": "awk",
    ".vim": "vim",
    ".rs.in": "rust",
    ".smali": "smali",
    ".ballerina": "ballerina", ".bal": "ballerina",
    ".pony": "pony",
    ".roc": "roc",
    ".koka": "koka",
    ".hoon": "hoon",
    ".wgsl": "wgsl",
    ".glsl": "glsl", ".vert": "glsl", ".frag": "glsl",
    ".hlsl": "hlsl",
    ".slang": "slang",
    ".ql": "ql",
    ".rego": "rego",
    ".tla": "tlaplus",

    # ---- shell / config-as-code
    ".sh": "bash", ".bash": "bash", ".ksh": "bash", ".zsh": "zsh",
    ".fish": "fish",
    ".ps1": "powershell", ".psm1": "powershell", ".psd1": "powershell",
    ".bat": "batch", ".cmd": "batch",
    ".nu": "nushell",
    ".elv": "elvish",
    ".tf": "terraform", ".tfvars": "terraform",
    ".hcl": "hcl",
    ".bzl": "starlark", ".star": "starlark",
    ".cmake": "cmake",
    ".mk": "make",
    ".just": "just",
    ".nix.in": "nix",

    # ---- data / schema / query (structure still worth graphing)
    ".sql": "sql", ".psql": "sql", ".pgsql": "sql",
    ".graphql": "graphql", ".gql": "graphql",
    ".proto": "proto",
    ".thrift": "thrift",
    ".capnp": "capnp",
    ".fbs": "flatbuffers",
    ".smithy": "smithy",
    ".prisma": "prisma",
    ".cue": "cue",
    ".pkl": "pkl",
    ".jsonnet": "jsonnet", ".libsonnet": "jsonnet",
    ".dhall": "dhall",
    ".wit": "wit",

    # ---- templates / markup whose logic matters
    ".vue": "vue",
    ".svelte": "svelte",
    ".astro": "astro",
    ".html": "html", ".htm": "html",
    ".erb": "embeddedtemplate",
    ".ejs": "embeddedtemplate",
    ".hbs": "glimmer", ".handlebars": "glimmer",
    ".twig": "twig",
    ".liquid": "liquid",
    ".blade.php": "blade",
    ".razor": "razor", ".cshtml": "razor",
    ".heex": "heex", ".eex": "eex",
    ".slim": "slim",
    ".haml": "haml",
    ".pug": "pug",
    ".jinja": "jinja2", ".j2": "jinja2",
    ".templ": "templ",
    ".qml": "qmljs",
}

# Files whose name, not suffix, says what they are.
FILENAME: dict[str, str] = {
    "Dockerfile": "dockerfile", "Containerfile": "dockerfile",
    "Makefile": "make", "GNUmakefile": "make", "makefile": "make",
    "CMakeLists.txt": "cmake",
    "Rakefile": "ruby", "Gemfile": "ruby", "Vagrantfile": "ruby",
    "Brewfile": "ruby", "Podfile": "ruby", "Fastfile": "ruby",
    "BUILD": "starlark", "BUILD.bazel": "starlark", "WORKSPACE": "starlark",
    "Justfile": "just", "justfile": "just",
    "SConstruct": "python", "SConscript": "python",
    "Jenkinsfile": "groovy",
    "go.mod": "gomod", "go.sum": "gosum", "go.work": "gowork",
    "Earthfile": "earthfile",
    "meson.build": "meson",
    ".env": "dotenv",
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
    "svelte": "js", "astro": "js", "html": "js", "qmljs": "js",
    "python": "py", "cython": "py", "mojo": "py",
    "c": "c", "cpp": "c", "objc": "c", "cuda": "c",
    "ocaml": "ml", "ocaml_interface": "ml", "reason": "ml",
    "fsharp": "fs", "fsharp_signature": "fs",
    "php": "php", "blade": "php",
    "ruby": "rb", "haml": "rb", "slim": "rb",
    "elixir": "ex", "heex": "ex", "eex": "ex",
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
