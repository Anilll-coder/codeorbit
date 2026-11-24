"""One extractor for every language CodeOrbit has a grammar for.

Writing a hand-tuned extractor per language does not scale past about three.
This one is driven by a `Spec` - which node types are definitions, which are
calls, which are imports - so a new language is a table entry, and a language
with no entry at all still works because `infer_spec` reads the node types out
of the grammar itself and classifies them by name.

That inference is the part worth defending. Tree-sitter grammars are written by
different people over a decade and they still converge hard on naming:
`function_definition`, `function_declaration`, `function_item`, `method_declaration`,
`class_declaration`, `struct_item`, `call_expression`, `import_declaration`. So
matching on the node-type name recovers most of the structure of a language
nobody here has ever looked at. It is not as good as a tuned spec - which is why
the twenty-odd languages people actually run this on have one - but the
alternative for the long tail is indexing nothing at all.

The deny list matters as much as the allow list. `class_body`, `function_type`,
`call_argument` and `enum_variant` all match the allow patterns and none of them
is a definition or a call, so a suffix deny list runs first.

What comes out is the same `Extraction` the Python and JavaScript extractors
produce: symbols, call sites, imports, base types. Resolution downstream does
not know or care which extractor produced them.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .base import CallSite, Extraction, ImportStmt, Symbol, line_of, text

# `vector<T>::push` -> `vector::push`; innermost first so nested args go too.
_TEMPLATE_ARGS = re.compile(r"<[^<>]*>")
_C_FAMILY = {"c", "cpp", "cuda", "objc"}


def _c_name(nm: str) -> str:
    """A C/C++ declarator's text reduced to the name it declares.

    The name field is a declarator, so its text can carry the return type's
    `&`/`*`, the parameter list, `const`, a leading macro
    (`FMT_CONSTEXPR width_checker`) or template arguments
    (`formatter<T, Char>`, cut off mid-line). Operators keep their symbol.
    """
    op = ""
    i = nm.find("operator")
    if i >= 0 and not re.match(r"[\w]", nm[i + 8:i + 9] or " "):
        nm, op = nm[:i], nm[i:]
        op = ("operator()" if re.match(r"operator\s*\(\s*\)", op)
              else re.split(r"\s*\(", op, maxsplit=1)[0])
        op = re.sub(r"\s+", "", op)
    else:
        nm = nm.split("(")[0]
    while _TEMPLATE_ARGS.search(nm):
        nm = _TEMPLATE_ARGS.sub("", nm)
    nm = nm.split("<")[0].strip()
    if " " in nm:
        nm = nm.split()[-1]
    nm = nm.lstrip("*&").strip()
    return nm + op


@dataclass(frozen=True)
class Spec:
    """What the walker should treat as a definition, a call or an import."""

    funcs: frozenset[str] = frozenset()
    classes: frozenset[str] = frozenset()
    calls: frozenset[str] = frozenset()
    imports: frozenset[str] = frozenset()
    # Fields tried in order when looking for a definition's name. The walker
    # follows them transitively, which is how C's
    # function_definition -> declarator -> declarator -> identifier is reached.
    name_fields: tuple[str, ...] = ("name", "declarator", "type", "path", "id")
    # Fields tried in order for the thing being called.
    call_fields: tuple[str, ...] = ("function", "name", "method", "callee",
                                    "target", "constructor")
    # Node types whose children name a base class / implemented interface.
    heritage: frozenset[str] = frozenset()
    # Calls to these names are recorded as imports instead (require, include).
    import_calls: frozenset[str] = frozenset()
    # Named scopes that are NOT themselves symbols. `impl Engine` in Rust does
    # not define Engine - the struct did - but everything inside it belongs to
    # Engine, and emitting a second `Engine` node would split the type in two.
    impl_scopes: frozenset[str] = frozenset()
    # Namespaces: a scope, not a type, so what they contain stays a function.
    namespaces: frozenset[str] = frozenset()
    # Subtrees to leave alone. Haskell's `signature` contains a node literally
    # typed `function` describing the *type*, which is not a definition.
    skip: frozenset[str] = frozenset()
    # Calls with no call node: the marker is an argument list, and the callee is
    # the nearest identifier before it. Dart works this way.
    sibling_calls: frozenset[str] = frozenset()
    # Field holding a method's receiver, when methods are declared at top level
    # and belong to a type only by virtue of it (Go).
    receiver_field: str | None = None
    hook: str | None = None          # name of a language-specific pre-pass


IDENT_TYPES = frozenset({
    "identifier", "type_identifier", "field_identifier", "simple_identifier",
    "property_identifier", "constant", "word", "IDENTIFIER", "name",
    "variable_name", "function_name", "method_name", "class_name",
    "dotted_name", "scoped_identifier", "qualified_identifier", "alias",
    "atom", "symbol", "package_identifier", "module_name", "term",
    "command_name", "identifier_reference", "type_name", "operator_name",
    "value_identifier", "label_name", "var_name", "shorthand_property_identifier",
})

# Node types that match an allow pattern but are never a definition or a call.
DENY_SUFFIXES = (
    "_body", "_block", "_list", "_type", "_types", "_parameter", "_parameters",
    "_argument", "_arguments", "_clause", "_modifier", "_modifiers", "_name",
    "_names", "_identifier", "_path", "_pattern", "_constraint", "_bound",
    "_bounds", "_field", "_fields", "_member", "_members", "_variant",
    "_constant", "_suffix", "_qualifier", "_reference", "_alias", "_literal",
    "_case", "_entry", "_value", "_spec_list", "_declarations", "_signature_item",
)

FUNC_PATTERNS = (
    "function_definition", "function_declaration", "function_item",
    "function_statement", "function_specification",
    "method_definition", "method_declaration", "method_spec", "method_elem",
    "subroutine_declaration_statement", "procedure_declaration",
    "procedure_definition", "constructor_declaration", "constructor_definition",
    "local_function_statement", "singleton_method", "macro_definition",
    "modifier_definition", "operator_declaration", "func_declaration",
    "fn_declaration", "create_function", "create_procedure",
    "function_signature", "secondary_constructor", "primary_constructor",
    # `_def` spellings: Crystal's method_def, Nim's proc, Scheme's define.
    "method_def", "func_def", "proc_def", "fun_def", "sub_def",
    "function_def", "macro_def", "routine_def", "define_function",
)
FUNC_EXACT = frozenset({"function", "method", "subroutine", "procedure", "def",
                        "function_clause", "FnProto", "routine", "proc", "fun",
                        "fn", "defun", "func", "define", "lambda"})

CLASS_PATTERNS = (
    "class_declaration", "class_definition", "class_specifier",
    "class_interface", "class_implementation", "struct_item",
    "struct_specifier", "struct_declaration", "struct_definition",
    "interface_declaration", "interface_definition", "trait_item",
    "trait_declaration", "trait_definition", "enum_item", "enum_declaration",
    "enum_specifier", "enum_definition", "object_declaration",
    "object_definition", "protocol_declaration", "impl_item",
    "module_definition", "record_declaration", "mixin_declaration",
    "union_specifier", "actor_declaration", "namespace_definition",
    "abstract_class_declaration",
    "class_def", "struct_def", "module_def", "enum_def", "trait_def",
    "object_def", "record_def", "protocol_def", "union_def", "actor_def",
    "interface_def",
)
CLASS_EXACT = frozenset({"class", "module", "struct", "interface", "trait",
                         "enum", "protocol", "object", "record", "type_spec"})

CALL_PATTERNS = (
    "call_expression", "call_statement", "function_call",
    "function_call_expression", "method_invocation", "invocation_expression",
    "method_call_expression", "member_call_expression",
    "scoped_call_expression", "macro_invocation", "macrocall_expression",
    "object_creation_expression", "new_expression", "constructor_invocation",
    "command_invocation", "procedure_call", "call_instruction",
)
CALL_EXACT = frozenset({"call", "invocation", "application", "command"})

IMPORT_PATTERNS = (
    "import_statement", "import_declaration", "import_from_statement",
    "import_header", "import_spec", "import_directive", "import_attribute",
    "foreign_import", "library_import", "module_import", "import_or_export",
    "use_declaration", "use_statement", "using_directive", "using_declaration",
    "preproc_include", "pp_include", "include_expression", "require_expression",
    "namespace_use_declaration", "open_module", "include_module",
    "import_or_export_statement", "package_import",
)
IMPORT_EXACT = frozenset({"import", "use", "require", "include", "open",
                          "using", "imports"})


def _classify(kind: str) -> str | None:
    """Which bucket a node type falls in, by its name alone."""
    if kind.endswith(DENY_SUFFIXES):
        return None
    if kind in FUNC_EXACT or any(p in kind for p in FUNC_PATTERNS):
        return "func"
    if kind in CLASS_EXACT or any(p in kind for p in CLASS_PATTERNS):
        return "class"
    if kind in IMPORT_EXACT or any(p in kind for p in IMPORT_PATTERNS):
        return "import"
    if kind in CALL_EXACT or any(p in kind for p in CALL_PATTERNS):
        return "call"
    return None


def infer_spec(language) -> Spec:
    """Build a Spec by reading the node types out of a grammar.

    The fallback for every language nobody has written a table entry for.
    """
    buckets: dict[str, set[str]] = {"func": set(), "class": set(),
                                    "call": set(), "import": set()}
    try:
        count = language.node_kind_count
    except Exception:      # noqa: BLE001 - a grammar that will not introspect
        return Spec()
    for i in range(count):
        try:
            if not language.node_kind_is_named(i):
                continue
            kind = language.node_kind_for_id(i)
        except Exception:  # noqa: BLE001
            continue
        bucket = _classify(kind)
        if bucket:
            buckets[bucket].add(kind)

    return Spec(
        funcs=frozenset(buckets["func"]),
        classes=frozenset(buckets["class"]),
        calls=frozenset(buckets["call"]),
        imports=frozenset(buckets["import"]),
    )


# ------------------------------------------------------------ tuned specs
#
# Hand-checked against a real file per language. Inference gets most of these
# right on its own; the entries exist where it does not - Go's named type is a
# `type_spec` inside an unnamed `type_declaration`, Rust's `impl_item` carries
# its name in a `type` field, C hides the name two declarators deep.

_SET_FIELDS = ("heritage", "import_calls", "impl_scopes", "namespaces",
               "skip", "sibling_calls")


def _s(funcs=(), classes=(), calls=(), imports=(), **kw) -> Spec:
    for f in _SET_FIELDS:
        if f in kw:
            kw[f] = frozenset(kw[f])
    return Spec(frozenset(funcs), frozenset(classes), frozenset(calls),
                frozenset(imports), **kw)


TUNED: dict[str, Spec] = {
    "go": _s(
        funcs=("function_declaration", "method_declaration", "method_elem"),
        classes=("type_spec",),
        calls=("call_expression",),
        imports=("import_spec",),
        receiver_field="receiver",
    ),
    "rust": _s(
        funcs=("function_item", "function_signature_item", "macro_definition"),
        classes=("struct_item", "enum_item", "trait_item", "union_item"),
        calls=("call_expression", "macro_invocation"),
        imports=("use_declaration",),
        name_fields=("name", "type", "path"),
        heritage=("trait",),
        impl_scopes=("impl_item",),
        namespaces=("mod_item",),
    ),
    "java": _s(
        funcs=("method_declaration", "constructor_declaration",
               "compact_constructor_declaration"),
        classes=("class_declaration", "interface_declaration",
                 "enum_declaration", "record_declaration",
                 "annotation_type_declaration"),
        calls=("method_invocation", "object_creation_expression",
               "explicit_constructor_invocation"),
        imports=("import_declaration",),
        heritage=("superclass", "super_interfaces"),
    ),
    "c": _s(
        funcs=("function_definition",),
        classes=("struct_specifier", "union_specifier", "enum_specifier"),
        calls=("call_expression",),
        imports=("preproc_include",),
    ),
    "cpp": _s(
        # Not template_function/template_method: those are uses such as
        # `static_cast<int>(x)`; a template's definition wraps an ordinary
        # function_definition, which this already finds.
        funcs=("function_definition",),
        classes=("class_specifier", "struct_specifier", "union_specifier",
                 "enum_specifier", "concept_definition"),
        namespaces=("namespace_definition",),
        calls=("call_expression", "new_expression"),
        imports=("preproc_include", "using_declaration"),
        heritage=("base_class_clause",),
    ),
    "objc": _s(
        funcs=("function_definition", "method_definition", "method_declaration"),
        classes=("class_interface", "class_implementation", "protocol_declaration",
                 "struct_specifier", "enum_specifier"),
        calls=("call_expression", "message_expression"),
        imports=("preproc_include", "module_import"),
    ),
    "csharp": _s(
        funcs=("method_declaration", "constructor_declaration",
               "local_function_statement", "destructor_declaration",
               "operator_declaration"),
        classes=("class_declaration", "interface_declaration",
                 "struct_declaration", "record_declaration",
                 "enum_declaration"),
        namespaces=("namespace_declaration", "file_scoped_namespace_declaration"),
        calls=("invocation_expression", "object_creation_expression"),
        imports=("using_directive",),
        heritage=("base_list",),
    ),
    "ruby": _s(
        funcs=("method", "singleton_method"),
        classes=("class", "module", "singleton_class"),
        calls=("call", "method_call"),
        imports=(),
        heritage=("superclass",),
        import_calls=("require", "require_relative", "load", "autoload"),
    ),
    "php": _s(
        funcs=("function_definition", "method_declaration"),
        classes=("class_declaration", "interface_declaration",
                 "trait_declaration", "enum_declaration"),
        calls=("function_call_expression", "member_call_expression",
               "scoped_call_expression", "object_creation_expression",
               "nullsafe_member_call_expression"),
        imports=("namespace_use_declaration", "include_expression",
                 "require_expression", "include_once_expression",
                 "require_once_expression"),
        heritage=("base_clause", "class_interface_clause"),
        namespaces=("namespace_definition",),
    ),
    "swift": _s(
        funcs=("function_declaration", "protocol_function_declaration",
               "init_declaration", "deinit_declaration", "macro_declaration"),
        classes=("class_declaration", "protocol_declaration",
                 "enum_declaration", "extension_declaration"),
        calls=("call_expression", "constructor_expression"),
        imports=("import_declaration",),
        heritage=("inheritance_specifier",),
    ),
    "kotlin": _s(
        funcs=("function_declaration", "secondary_constructor",
               "primary_constructor", "anonymous_function"),
        classes=("class_declaration", "object_declaration", "companion_object"),
        calls=("call_expression", "constructor_invocation"),
        imports=("import_header",),
        heritage=("delegation_specifier",),
    ),
    "scala": _s(
        funcs=("function_definition", "function_declaration"),
        classes=("class_definition", "object_definition", "trait_definition",
                 "enum_definition", "given_definition"),
        calls=("call_expression",),
        imports=("import_declaration",),
        heritage=("extends_clause",),
    ),
    "dart": _s(
        funcs=("function_signature", "method_signature", "getter_signature",
               "setter_signature", "constructor_signature",
               "local_function_declaration"),
        classes=("class_definition", "mixin_declaration", "enum_declaration",
                 "extension_declaration"),
        calls=("new_expression", "const_object_expression"),
        sibling_calls=("argument_part",),
        imports=("import_or_export", "library_import"),
        heritage=("superclass", "interfaces"),
    ),
    "lua": _s(
        funcs=("function_declaration", "function_definition"),
        classes=(),
        calls=("function_call",),
        imports=(),
        import_calls=("require",),
    ),
    "julia": _s(
        funcs=("function_definition", "macro_definition", "short_function_definition"),
        classes=("struct_definition", "abstract_definition", "module_definition"),
        calls=("call_expression", "macrocall_expression", "broadcast_call_expression"),
        imports=("import_statement", "using_statement"),
    ),
    "r": _s(
        funcs=("function_definition",),
        calls=("call",),
        import_calls=("library", "require", "source"),
    ),
    "perl": _s(
        funcs=("subroutine_declaration_statement", "method_declaration_statement"),
        classes=("package_statement", "class_statement"),
        calls=("function_call_expression", "method_call_expression",
               "ambiguous_function_call_expression"),
        imports=("use_statement", "require_expression"),
    ),
    "haskell": _s(
        funcs=("function", "bind"),
        classes=("class", "data_type", "newtype"),
        calls=("apply",),
        imports=("import",),
        skip=("signature", "type_alias", "data_type"),
    ),
    "erlang": _s(
        funcs=("fun_decl", "function_clause"),
        classes=(),
        calls=("call",),
        imports=("import_attribute", "pp_include", "pp_include_lib"),
    ),
    "elixir": _s(calls=("call",), hook="elixir"),
    "bash": _s(funcs=("function_definition",), calls=("command",),
               import_calls=("source", ".")),
    "zsh": _s(funcs=("function_definition",), calls=("command",)),
    "solidity": _s(
        funcs=("function_definition", "constructor_definition",
               "modifier_definition", "fallback_receive_definition"),
        classes=("contract_declaration", "interface_declaration",
                 "library_declaration", "struct_declaration", "enum_declaration"),
        calls=("call_expression",),
        imports=("import_directive",),
        heritage=("inheritance_specifier",),
    ),
    "zig": _s(funcs=("FnProto",), classes=("ContainerDecl",),
              calls=("SuffixExpr",), name_fields=("name",)),
    "elm": _s(funcs=("value_declaration",), classes=("type_declaration",),
              calls=("function_call_expr",), imports=("import_clause",)),
    "ocaml": _s(funcs=("let_binding", "method_definition"),
                classes=("class_definition", "module_definition", "type_definition"),
                calls=("application_expression",),
                imports=("open_module", "include_module")),
    "gdscript": _s(funcs=("function_definition",), classes=("class_definition",),
                   calls=("call",), imports=("extends_statement",)),
    "groovy": _s(funcs=("function_definition",), classes=("class_definition",),
                 calls=("function_call", "juxt_function_call")),
    "terraform": _s(classes=("block",), calls=("function_call",)),
    "hcl": _s(classes=("block",), calls=("function_call",)),
}


# ---------------------------------------------------------------- helpers

def _first_field(node, fields):
    for f in fields:
        c = node.child_by_field_name(f)
        if c is not None:
            return c
    return None


def _name_of(node, src: bytes, spec: Spec) -> str | None:
    """A definition's name, following name-ish fields down to an identifier.

    C is the reason this is a loop rather than one lookup: a
    `function_definition`'s name sits under declarator -> declarator ->
    identifier, and a single `child_by_field_name("name")` finds nothing there.
    """
    cur = node
    for _ in range(6):
        nxt = _first_field(cur, spec.name_fields)
        if nxt is None:
            break
        cur = nxt
        if cur.type in IDENT_TYPES:
            return text(cur, src).strip()[:120] or None

    if cur is not node:
        raw = text(cur, src).strip()
        if raw and "\n" not in raw and len(raw) <= 120:
            return raw

    # Nothing named itself. Take the first identifier-ish child, which is what
    # grammars without name fields (Zig, some Lisps) leave us.
    for child in node.named_children[:4]:
        if child.type in IDENT_TYPES:
            return text(child, src).strip()[:120] or None
    return None


def _split_qualified(raw: str) -> tuple[str, str | None]:
    """`a.b.c()` / `A::b()` / `x->y()` -> ("c" | "b" | "y", the prefix).

    Deliberately textual. Every grammar spells a member call differently -
    member_expression, selector_expression, field_expression,
    scoped_call_expression - and every one of them renders to the same text.
    """
    raw = raw.strip().split("(")[0].split("<")[0].split("[")[0].strip()
    for sep in ("->", "::", "."):
        if sep in raw:
            head, _, tail = raw.rpartition(sep)
            tail = tail.strip()
            if tail:
                return tail, (head.strip()[:80] or None)
    return raw, None


def _callee_of(node, src: bytes, spec: Spec) -> tuple[str, str | None] | None:
    """(callee name, receiver) for a call node."""
    fn = _first_field(node, spec.call_fields)
    if fn is None:
        # Grammars with no field on the call node (Kotlin, Elixir, Ruby's
        # `call` in some shapes) put the callee first.
        fn = node.named_children[0] if node.named_children else None
    if fn is None:
        return None

    # A two-field member call gives the split for free, and more reliably than
    # text: the object may itself be a whole expression.
    prop = _first_field(fn, ("property", "field", "name", "attribute", "method"))
    obj = _first_field(fn, ("object", "operand", "value", "receiver", "argument"))
    if prop is not None and prop.type in IDENT_TYPES:
        return (text(prop, src).strip()[:120],
                text(obj, src).strip()[:80] if obj is not None else None)

    raw = text(fn, src)
    if "\n" in raw or len(raw) > 200:
        return None
    name, recv = _split_qualified(raw)
    if not name or not name.replace("_", "").replace("$", "").isalnum():
        return None
    return name, recv


def _sibling_callee(node, src: bytes) -> str | None:
    """Callee for a grammar with no call node, only an argument list.

    Dart spells `helper()` as `identifier` followed by a `selector` holding an
    `argument_part`. The call is real and so is the edge, but there is no node
    that means "call", so the name has to be read off the nearest preceding
    sibling - of the argument list, or of the selector wrapping it.
    """
    for level in (node, node.parent):
        if level is None:
            continue
        prev = level.prev_named_sibling
        while prev is not None:
            if prev.type in IDENT_TYPES:
                return text(prev, src).strip()[:120] or None
            if prev.type.endswith(("_expression", "selector")):
                name, _ = _split_qualified(text(prev, src))
                return name or None
            prev = prev.prev_named_sibling
    return None


def _receiver_type(node, src: bytes, spec: Spec) -> str | None:
    """The type a top-level method hangs off, for languages that declare it
    through a receiver rather than by nesting (Go: `func (s *Server) Start()`)."""
    if not spec.receiver_field:
        return None
    recv = node.child_by_field_name(spec.receiver_field)
    if recv is None:
        return None
    raw = text(recv, src).strip("()").strip()
    # "s *Server" / "*Server" / "s Server[T]" -> Server
    token = raw.split()[-1] if raw.split() else ""
    token = token.lstrip("*&").split("[")[0].split("<")[0].rsplit(".", 1)[-1]
    return token or None


def _call_argument(node, src: bytes) -> str:
    """The first argument of a call, for `require "json"`-style imports."""
    args = _first_field(node, ("arguments", "argument", "parameters"))
    if args is None:
        args = next((c for c in node.named_children
                     if c.type in ("arguments", "argument_list", "word")), None)
    target = args if args is not None else node
    stack = [target]
    while stack:
        n = stack.pop(0)
        if "string" in n.type or n.type in ("word", "raw_string_literal"):
            body = text(n, src).strip().strip("\"'`")
            if body:
                return body[:200]
        stack.extend(n.named_children[:4])
    return text(target, src).strip().strip("\"'")[:200]


def _module_of(node, src: bytes) -> str:
    """The module an import statement refers to, best effort."""
    named = _first_field(node, ("source", "module_name", "path", "name",
                                "argument", "module", "library"))
    if named is not None:
        return text(named, src).strip().strip("\"'<>`;")[:200]

    # Otherwise the first string or identifier under the node - which covers
    # `#include <stdio.h>`, `import "fmt"` and `use std::fmt` alike.
    stack = list(node.named_children)
    while stack:
        n = stack.pop(0)
        if "string" in n.type or n.type in IDENT_TYPES or "path" in n.type:
            return text(n, src).strip().strip("\"'<>`;")[:200]
        stack.extend(n.named_children[:3])
    return text(node, src).strip().split("\n")[0][:200]


# --------------------------------------------------------------- the walk

def extract(tree, src: bytes, module_qname: str, lang: str, spec: Spec) -> Extraction:
    out = Extraction()
    root = tree.root_node

    out.symbols.append(Symbol(
        name=module_qname.rsplit(".", 1)[-1], qname=module_qname, kind="module",
        start_line=1, end_line=root.end_point[0] + 1,
    ))

    seen_qnames: set[str] = {module_qname}

    def unique(qname: str) -> str:
        """Overloads and same-named nested defs are real; collisions are not.

        C++ and Java overload freely, and `impl` blocks in Rust repeat method
        names across types. Two symbols with one qname would make the graph
        silently merge them, so the second onward gets a discriminator.
        """
        if qname not in seen_qnames:
            seen_qnames.add(qname)
            return qname
        i = 2
        while f"{qname}#{i}" in seen_qnames:
            i += 1
        seen_qnames.add(f"{qname}#{i}")
        return f"{qname}#{i}"

    def heritage(node, qname: str) -> None:
        for child in node.named_children:
            if child.type not in spec.heritage:
                continue
            for part in text(child, src).replace(":", " ").split():
                part = part.strip(",<>()")
                if not part or part in ("extends", "implements", "public",
                                        "private", "protected", "virtual"):
                    continue
                out.base_types.append((qname, part.rsplit(".", 1)[-1]
                                       .rsplit("::", 1)[-1]))

    def signature(node, nm: str) -> str | None:
        params = _first_field(node, ("parameters", "parameter_list",
                                     "function_value_parameters", "arguments"))
        if params is None:
            return None
        return (nm + text(params, src).replace("\n", " "))[:300]

    def walk(node, scope: str, in_class: bool) -> None:
        for child in node.named_children:
            t = child.type

            if t in spec.skip:
                continue

            if t in spec.sibling_calls:
                hit = _sibling_callee(child, src)
                if hit is not None:
                    out.calls.append(CallSite(scope, hit, None, line_of(child)))
                walk(child, scope, in_class)
                continue

            if t in spec.funcs:
                nm = _name_of(child, src, spec)
                if nm is None:
                    walk(child, scope, in_class)
                    continue
                if lang in _C_FAMILY:
                    nm = _c_name(nm)
                    if not nm:
                        walk(child, scope, in_class)
                        continue
                owner = _receiver_type(child, src, spec)
                if "::" in nm:
                    # C++ defines methods out of line: `void buffer::push()`,
                    # `S::~S()`, `ns::f()`. The qualifier is scope, not name,
                    # or name search for `push` and the call graph miss it.
                    bare = nm
                    while _TEMPLATE_ARGS.search(bare):
                        bare = _TEMPLATE_ARGS.sub("", bare)
                    parts = [p for p in bare.split("::") if p]
                    if len(parts) > 1:
                        nm = parts[-1]
                        owner = ".".join(parts[:-1])
                # A Go method is declared at top level and belongs to its type
                # only through the receiver. Without this every method in the
                # package lands beside the functions, `Server.Start` does not
                # exist as a name, and two types with a `Close` collide.
                parent_scope = f"{scope}.{owner}" if owner else scope
                q = unique(f"{parent_scope}.{nm}")
                out.symbols.append(Symbol(
                    name=nm, qname=q,
                    kind="method" if (in_class or owner) else "function",
                    start_line=line_of(child), end_line=child.end_point[0] + 1,
                    signature=signature(child, nm), parent=parent_scope,
                ))
                walk(child, q, False)
                continue

            if t in spec.impl_scopes or t in spec.namespaces:
                nm = _name_of(child, src, spec)
                if nm is None:
                    walk(child, scope, in_class)
                    continue
                q = f"{scope}.{nm.rsplit('::', 1)[-1]}"
                heritage(child, q)
                walk(child, q, t in spec.impl_scopes)
                continue

            if t in spec.classes:
                nm = _name_of(child, src, spec)
                if nm is not None and lang in _C_FAMILY:
                    nm = _c_name(nm).rsplit("::", 1)[-1] or None
                if nm is None:
                    walk(child, scope, in_class)
                    continue
                q = unique(f"{scope}.{nm}")
                out.symbols.append(Symbol(
                    name=nm, qname=q, kind="class",
                    start_line=line_of(child), end_line=child.end_point[0] + 1,
                    parent=scope,
                ))
                heritage(child, q)
                walk(child, q, True)
                continue

            if t in spec.imports:
                out.imports.append(ImportStmt(
                    _module_of(child, src), None, None, line_of(child)))
                continue

            if t in spec.calls:
                hit = _callee_of(child, src, spec)
                if hit is not None:
                    name, recv = hit
                    if name in spec.import_calls:
                        # `require "json"`, `source ./lib.sh`: spelled as a call,
                        # but it is an import and the module view needs it there.
                        out.imports.append(ImportStmt(
                            _call_argument(child, src), None, None, line_of(child)))
                    else:
                        out.calls.append(CallSite(scope, name, recv, line_of(child)))
                walk(child, scope, in_class)
                continue

            walk(child, scope, in_class)

    if spec.hook == "elixir":
        _elixir(root, src, out, module_qname, unique)
    else:
        walk(root, module_qname, False)
    return out


# ---------------------------------------------------------------- elixir
#
# Elixir has no definition node at all: `defmodule`, `def` and `defp` are
# ordinary calls to macros, so the generic walk sees a file of function calls
# and no functions. Matching on the macro name is the only way in.

_ELIXIR_DEF = {"def", "defp", "defmacro", "defmacrop", "defdelegate", "defn"}
_ELIXIR_MOD = {"defmodule", "defprotocol", "defimpl"}
_ELIXIR_IMPORT = {"import", "alias", "require", "use"}


def _elixir(root, src: bytes, out: Extraction, module_qname: str, unique) -> None:
    def call_target(node) -> str | None:
        fn = node.child_by_field_name("target")
        if fn is None:
            fn = node.named_children[0] if node.named_children else None
        return (text(fn, src).strip()
                if fn is not None and fn.type == "identifier" else None)

    def first_arg_name(node) -> str | None:
        args = next((c for c in node.named_children if c.type == "arguments"), None)
        if args is None or not args.named_children:
            return None
        first = args.named_children[0]
        if first.type == "call":                 # def go(x) -> call go(x)
            inner = first.child_by_field_name("target") or (
                first.named_children[0] if first.named_children else None)
            return text(inner, src).strip()[:120] if inner is not None else None
        if first.type == "binary_operator":      # def go(x) when is_int(x)
            left = first.named_children[0] if first.named_children else None
            if left is not None and left.type == "call":
                inner = left.child_by_field_name("target")
                if inner is not None:
                    return text(inner, src).strip()[:120]
        return text(first, src).strip()[:120] or None

    def walk(node, scope: str, in_module: bool) -> None:
        for child in node.named_children:
            if child.type != "call":
                walk(child, scope, in_module)
                continue

            target = call_target(child)

            if target in _ELIXIR_MOD:
                nm = (first_arg_name(child) or "?").lstrip(":")
                q = unique(f"{scope}.{nm}")
                out.symbols.append(Symbol(
                    name=nm.rsplit(".", 1)[-1], qname=q, kind="class",
                    start_line=line_of(child), end_line=child.end_point[0] + 1,
                    parent=scope,
                ))
                walk(child, q, True)
                continue

            if target in _ELIXIR_DEF:
                nm = first_arg_name(child)
                if nm:
                    q = unique(f"{scope}.{nm}")
                    out.symbols.append(Symbol(
                        name=nm, qname=q,
                        kind="method" if in_module else "function",
                        start_line=line_of(child), end_line=child.end_point[0] + 1,
                        parent=scope,
                    ))
                    walk(child, q, False)
                    continue

            if target in _ELIXIR_IMPORT:
                out.imports.append(ImportStmt(
                    (first_arg_name(child) or "?"), None, None, line_of(child)))
                continue

            if target:
                out.calls.append(CallSite(scope, target, None, line_of(child)))
            else:
                # `Mod.fun(x)` - the callee is a dot expression, not an identifier.
                fn = child.named_children[0] if child.named_children else None
                if fn is not None:
                    name, recv = _split_qualified(text(fn, src))
                    if name:
                        out.calls.append(CallSite(scope, name, recv, line_of(child)))
            walk(child, scope, in_module)

    walk(root, module_qname, False)
