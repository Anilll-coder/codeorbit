"""TypeScript / TSX extractor.

TypeScript's grammar is a superset of JavaScript's, so everything the JS
extractor understands - function declarations, arrows bound to a const, class
methods, object-property functions, calls, imports - is reused rather than
reimplemented. Two extractors for one language family would drift, and the JS
one is already the tested path.

What is added here is the part that has no JavaScript equivalent, and it is the
part that matters most for a graph: TypeScript states relationships that JS only
implies. An `interface` names a contract, `implements` names who honours it, and
a `type` alias names a shape - all of which are edges a reader follows and a
call graph alone cannot show.

  interface_declaration        -> an interface symbol, its methods as members
  type_alias_declaration       -> a type symbol
  enum_declaration             -> an enum symbol, its members
  abstract_class_declaration   -> a class (abstract is not a different thing
                                  to point at)
  implements_clause            -> the same `extends` edge kind: both say "this
                                  type is a kind of that one", which is the
                                  question a reader is asking
  method_signature             -> a method on an interface, so a call to it
                                  resolves somewhere rather than dangling
"""
from __future__ import annotations

from . import javascript_ex
from .base import Extraction, Symbol, line_of, text

TYPE_DECLS = {
    "interface_declaration": "interface",
    "type_alias_declaration": "type_alias",
    "enum_declaration": "enum",
}


def _members_of(node, src: bytes, owner: str, out: Extraction) -> None:
    """Methods and properties declared inside an interface or enum body."""
    body = node.child_by_field_name("body")
    if body is None:
        return
    for child in body.named_children:
        if child.type in ("method_signature", "method_definition"):
            nm = child.child_by_field_name("name")
            if nm is None:
                continue
            name = text(nm, src)
            out.symbols.append(Symbol(
                name=name, qname=f"{owner}.{name}", kind="method",
                start_line=line_of(child), end_line=child.end_point[0] + 1,
                signature=javascript_ex._params(child, src) and
                (name + javascript_ex._params(child, src)) or name,
                parent=owner,
            ))
        elif child.type in ("property_signature", "enum_assignment",
                            "public_field_definition"):
            nm = child.child_by_field_name("name")
            if nm is None:
                # An enum member with no explicit value is a bare identifier.
                nm = next((c for c in child.named_children
                           if c.type in ("identifier", "property_identifier")), None)
            if nm is None:
                continue
            name = text(nm, src)
            out.symbols.append(Symbol(
                name=name, qname=f"{owner}.{name}", kind="property",
                start_line=line_of(child), end_line=child.end_point[0] + 1,
                parent=owner,
            ))
        elif child.type in ("identifier", "property_identifier"):
            # `enum Kind { A, B }` - members are bare identifiers.
            name = text(child, src)
            out.symbols.append(Symbol(
                name=name, qname=f"{owner}.{name}", kind="enum_member",
                start_line=line_of(child), end_line=child.end_point[0] + 1,
                parent=owner,
            ))


def _implements(node, src: bytes, owner: str, out: Extraction) -> None:
    """`class C implements A, B` - recorded with the same edge kind as extends.

    A reader asking "what is this a kind of" does not distinguish the two, and
    collapsing them keeps one question answerable with one query.
    """
    # The clause sits inside a `class_heritage` wrapper, not directly under the
    # class - looking only at direct children finds nothing.
    for child in node.named_children:
        clauses = []
        if child.type == "implements_clause":
            clauses = [child]
        elif child.type == "class_heritage":
            clauses = [c for c in child.named_children
                       if c.type == "implements_clause"]
        for clause in clauses:
            for t in clause.named_children:
                base = text(t, src).split("<")[0].strip().rsplit(".", 1)[-1]
                if base:
                    out.base_types.append((owner, base))


def extract(tree, src: bytes, module_qname: str) -> Extraction:
    # Everything JavaScript-shaped, from the extractor that already handles it.
    out = javascript_ex.extract(tree, src, module_qname)

    root = tree.root_node

    def walk(node, scope: str) -> None:
        for child in node.named_children:
            t = child.type

            if t in TYPE_DECLS:
                nm = child.child_by_field_name("name")
                if nm is None:
                    walk(child, scope)
                    continue
                name = text(nm, src)
                qname = f"{scope}.{name}"
                out.symbols.append(Symbol(
                    name=name, qname=qname, kind=TYPE_DECLS[t],
                    start_line=line_of(child), end_line=child.end_point[0] + 1,
                    parent=scope,
                ))
                # An interface can extend other interfaces.
                for c in child.named_children:
                    if c.type == "extends_type_clause":
                        for tt in c.named_children:
                            base = text(tt, src).split("<")[0].strip().rsplit(".", 1)[-1]
                            if base:
                                out.base_types.append((qname, base))
                _members_of(child, src, qname, out)
                continue

            if t == "abstract_class_declaration":
                nm = child.child_by_field_name("name")
                name = text(nm, src) if nm else "(anonymous)"
                qname = f"{scope}.{name}"
                out.symbols.append(Symbol(
                    name=name, qname=qname, kind="class",
                    start_line=line_of(child), end_line=child.end_point[0] + 1,
                    parent=scope,
                ))
                _implements(child, src, qname, out)
                walk(child, qname)
                continue

            if t in ("class_declaration", "class"):
                nm = child.child_by_field_name("name")
                if nm is not None:
                    _implements(child, src, f"{scope}.{text(nm, src)}", out)

            if t == "export_statement":
                walk(child, scope)
                continue

            walk(child, scope)

    walk(root, module_qname)

    # Both passes can record the same inheritance edge; keep one of each.
    out.base_types = list(dict.fromkeys(out.base_types))

    # The JS pass and this one can both reach a class inside an export; keep the
    # first of any duplicate qname so a symbol is defined once.
    seen: set[str] = set()
    unique = []
    for s in out.symbols:
        if s.qname in seen:
            continue
        seen.add(s.qname)
        unique.append(s)
    out.symbols = unique

    return out
