"""JavaScript/JSX extractor.

Harder than Python: a function can be a declaration, an arrow bound to a const,
a class method, or an object property. All four are real definitions a caller
can land on, so all four become nodes.
"""
from __future__ import annotations

from .base import CallSite, Extraction, ImportStmt, Symbol, line_of, text

FUNCEXPR = {"function_expression", "arrow_function", "function"}


def _params(node, src: bytes) -> str:
    p = node.child_by_field_name("parameters")
    if p is not None:
        return text(p, src).replace("\n", " ")[:200]
    p = node.child_by_field_name("parameter")
    return "(" + text(p, src) + ")" if p is not None else "()"


def extract(tree, src: bytes, module_qname: str) -> Extraction:
    out = Extraction()
    root = tree.root_node

    out.symbols.append(Symbol(
        name=module_qname.rsplit(".", 1)[-1], qname=module_qname, kind="module",
        start_line=1, end_line=root.end_point[0] + 1,
    ))

    def add(nm, qname, kind, node, scope, sig=None):
        out.symbols.append(Symbol(
            name=nm, qname=qname, kind=kind,
            start_line=line_of(node), end_line=node.end_point[0] + 1,
            signature=sig, parent=scope,
        ))

    def walk(node, scope: str, in_class: bool) -> None:
        for child in node.named_children:
            t = child.type

            if t in ("function_declaration", "generator_function_declaration"):
                nm_node = child.child_by_field_name("name")
                nm = text(nm_node, src) if nm_node else "(anonymous)"
                q = scope + "." + nm
                add(nm, q, "function", child, scope, nm + _params(child, src))
                walk(child, q, False)
                continue

            # abstract_class_declaration only occurs in TypeScript, which reuses
            # this walker. Handling it here rather than in the TS extractor is
            # what keeps a method's qualified name right: without it the walker
            # descends into the class as an unknown node and every method lands
            # at module scope instead of on its class.
            if t in ("class_declaration", "class", "abstract_class_declaration"):
                nm_node = child.child_by_field_name("name")
                nm = text(nm_node, src) if nm_node else "(anonymous)"
                q = scope + "." + nm
                add(nm, q, "class", child, scope)
                sup = child.child_by_field_name("superclass")
                if sup is not None:
                    out.base_types.append((q, text(sup, src).rsplit(".", 1)[-1]))
                else:
                    for c in child.named_children:
                        if c.type != "class_heritage":
                            continue
                        # Take only the extends part. `class A extends B
                        # implements C` has both under one heritage node, and
                        # stripping just the word "extends" from its text left
                        # "B implements C" recorded as a single base name.
                        raw = text(c, src)
                        for clause in c.named_children:
                            if clause.type == "extends_clause":
                                raw = text(clause, src)
                                break
                        base = raw.replace("extends", "").split("implements")[0]
                        base = base.split("(")[0].split("<")[0].strip()
                        if base:
                            out.base_types.append((q, base.rsplit(".", 1)[-1]))
                walk(child, q, True)
                continue

            if t == "method_definition":
                nm_node = child.child_by_field_name("name")
                nm = text(nm_node, src) if nm_node else "(anonymous)"
                q = scope + "." + nm
                add(nm, q, "method", child, scope, nm + _params(child, src))
                walk(child, q, False)
                continue

            if t == "variable_declarator":
                nm_node = child.child_by_field_name("name")
                val = child.child_by_field_name("value")
                if nm_node is not None and val is not None and val.type in FUNCEXPR:
                    nm = text(nm_node, src)
                    q = scope + "." + nm
                    add(nm, q, "function", child, scope, nm + _params(val, src))
                    walk(val, q, False)
                    continue

            if t == "pair":
                key = child.child_by_field_name("key")
                val = child.child_by_field_name("value")
                if key is not None and val is not None and val.type in FUNCEXPR:
                    nm = text(key, src).strip("\"'")
                    q = scope + "." + nm
                    add(nm, q, "function", child, scope, nm + _params(val, src))
                    walk(val, q, False)
                    continue

            if t == "call_expression":
                fn = child.child_by_field_name("function")
                if fn is not None:
                    if fn.type == "identifier":
                        out.calls.append(CallSite(scope, text(fn, src), None, line_of(child)))
                    elif fn.type == "member_expression":
                        prop = fn.child_by_field_name("property")
                        obj = fn.child_by_field_name("object")
                        if prop is not None:
                            recv = text(obj, src)[:80] if obj is not None else None
                            out.calls.append(CallSite(scope, text(prop, src), recv, line_of(child)))
                walk(child, scope, in_class)
                continue

            if t == "new_expression":
                ctor = child.child_by_field_name("constructor")
                if ctor is not None:
                    nm = text(ctor, src).rsplit(".", 1)[-1]
                    out.calls.append(CallSite(scope, nm, None, line_of(child)))
                walk(child, scope, in_class)
                continue

            if t == "import_statement":
                _import(child, src, out)
                continue

            walk(child, scope, in_class)

    walk(root, module_qname, False)
    return out


def _import(node, src: bytes, out: Extraction) -> None:
    ln = line_of(node)
    src_node = node.child_by_field_name("source")
    module = text(src_node, src).strip("\"'") if src_node is not None else "?"
    clause = None
    for c in node.named_children:
        if c.type == "import_clause":
            clause = c
    if clause is None:
        out.imports.append(ImportStmt(module, None, None, ln))
        return
    for c in clause.named_children:
        if c.type == "identifier":
            out.imports.append(ImportStmt(module, "default", text(c, src), ln))
        elif c.type == "named_imports":
            for spec in c.named_children:
                if spec.type != "import_specifier":
                    continue
                nm = spec.child_by_field_name("name")
                al = spec.child_by_field_name("alias")
                out.imports.append(ImportStmt(
                    module, text(nm, src) if nm else None,
                    text(al, src) if al else None, ln))
        elif c.type == "namespace_import":
            ident = next((k for k in c.named_children if k.type == "identifier"), None)
            out.imports.append(ImportStmt(module, "*", text(ident, src) if ident else None, ln))
