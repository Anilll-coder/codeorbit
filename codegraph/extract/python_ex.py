"""Python extractor: symbols, calls, imports and base classes from a tree-sitter AST."""
from __future__ import annotations

from .base import CallSite, Extraction, ImportStmt, Symbol, line_of, text

DEF_KINDS = {"function_definition", "class_definition"}


def _docstring(body, src: bytes) -> str | None:
    """PEP 257: the first statement of a body, if it is a bare string."""
    if body is None or not body.named_children:
        return None
    first = body.named_children[0]
    if first.type == "expression_statement" and first.named_children:
        s = first.named_children[0]
        if s.type == "string":
            raw = text(s, src).strip()
            for q in ('"""', "'''", '"', "'"):
                if raw.startswith(q) and raw.endswith(q) and len(raw) >= 2 * len(q):
                    return raw[len(q):-len(q)].strip()[:500]
            return raw[:500]
    return None


def _signature(node, src: bytes) -> str:
    name = node.child_by_field_name("name")
    params = node.child_by_field_name("parameters")
    ret = node.child_by_field_name("return_type")
    out = text(name, src) if name else "?"
    if params is not None:
        out += text(params, src)
    if ret is not None:
        out += " -> " + text(ret, src)
    return out.replace("\n", " ")[:300]


def extract(tree, src: bytes, module_qname: str) -> Extraction:
    out = Extraction()
    root = tree.root_node

    out.symbols.append(Symbol(
        name=module_qname.rsplit(".", 1)[-1], qname=module_qname, kind="module",
        start_line=1, end_line=root.end_point[0] + 1,
        docstring=_docstring(root, src),
    ))

    def walk(node, scope: str, cls_depth: int) -> None:
        for child in node.named_children:
            t = child.type

            if t in DEF_KINDS:
                nm_node = child.child_by_field_name("name")
                if nm_node is None:
                    walk(child, scope, cls_depth)
                    continue
                nm = text(nm_node, src)
                qname = f"{scope}.{nm}"
                is_class = t == "class_definition"
                body = child.child_by_field_name("body")
                kind = "class" if is_class else ("method" if cls_depth > 0 else "function")

                out.symbols.append(Symbol(
                    name=nm, qname=qname, kind=kind,
                    start_line=line_of(child), end_line=child.end_point[0] + 1,
                    signature=None if is_class else _signature(child, src),
                    docstring=_docstring(body, src),
                    parent=scope,
                ))

                if is_class:
                    supers = child.child_by_field_name("superclasses")
                    if supers is not None:
                        for arg in supers.named_children:
                            base = text(arg, src).split("(")[0].strip()
                            if base:
                                out.base_types.append((qname, base.rsplit(".", 1)[-1]))

                walk(child, qname, cls_depth + 1 if is_class else 0)
                continue

            if t == "call":
                fn = child.child_by_field_name("function")
                if fn is not None:
                    if fn.type == "identifier":
                        out.calls.append(CallSite(scope, text(fn, src), None, line_of(child)))
                    elif fn.type == "attribute":
                        attr = fn.child_by_field_name("attribute")
                        obj = fn.child_by_field_name("object")
                        if attr is not None:
                            out.calls.append(CallSite(
                                scope, text(attr, src),
                                text(obj, src)[:80] if obj is not None else None,
                                line_of(child),
                            ))
                walk(child, scope, cls_depth)
                continue

            if t in ("import_statement", "import_from_statement"):
                _imports(child, src, out)
                continue

            walk(child, scope, cls_depth)

    walk(root, module_qname, 0)
    return out


def _imports(node, src: bytes, out: Extraction) -> None:
    ln = line_of(node)
    if node.type == "import_statement":
        for child in node.named_children:
            if child.type == "dotted_name":
                out.imports.append(ImportStmt(text(child, src), None, None, ln))
            elif child.type == "aliased_import":
                nm = child.child_by_field_name("name")
                al = child.child_by_field_name("alias")
                out.imports.append(ImportStmt(
                    text(nm, src) if nm else "?", None,
                    text(al, src) if al else None, ln))
        return

    mod_node = node.child_by_field_name("module_name")
    module = text(mod_node, src) if mod_node is not None else "."
    named = [c for c in node.named_children if c is not mod_node]
    if not named:
        out.imports.append(ImportStmt(module, None, None, ln))
        return
    for c in named:
        if c.type == "dotted_name" or c.type == "identifier":
            out.imports.append(ImportStmt(module, text(c, src), None, ln))
        elif c.type == "aliased_import":
            nm = c.child_by_field_name("name")
            al = c.child_by_field_name("alias")
            out.imports.append(ImportStmt(
                module, text(nm, src) if nm else None,
                text(al, src) if al else None, ln))
        elif c.type == "wildcard_import":
            out.imports.append(ImportStmt(module, "*", None, ln))
