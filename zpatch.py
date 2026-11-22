"""Classify a Python source edit as code-object swaps on the already imported pin.

A supported edit keeps every module and class statement the same once function bodies,
docstrings and literal argument defaults are set aside. The result lists the code objects
whose own instructions or constants change, the function trees to swap so new closures pick
up the edit, and docstring and default updates.
"""

import ast
import types

NEWLOCALS = 0x2


class Unsupported(Exception):
    pass


def _doc_index(body):
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
            and isinstance(body[0].value.value, str):
        return 0
    return None


class _Normalize(ast.NodeTransformer):
    """Remove the parts handled by swaps: function bodies, docstrings and literal defaults."""

    def __init__(self):
        self.docs = []
        self.defaults = []
        self.scope = []

    def _strip_doc(self, node, kind):
        i = _doc_index(node.body)
        doc = None
        if i is not None:
            doc = node.body[0].value.value
            node.body = node.body[1:]
        self.docs.append((kind, ".".join(self.scope), doc))

    def visit_Module(self, node):
        self._strip_doc(node, "module")
        self.generic_visit(node)
        return node

    def visit_ClassDef(self, node):
        self.scope.append(node.name)
        self._strip_doc(node, "class")
        self.generic_visit(node)
        self.scope.pop()
        return node

    def _function(self, node):
        args = node.args
        values = []
        for d in args.defaults:
            values.append(ast.literal_eval(d) if _literal(d) else _Opaque(ast.dump(d)))
        kw = []
        for d in args.kw_defaults:
            kw.append(None if d is None else ast.literal_eval(d) if _literal(d) else _Opaque(ast.dump(d)))
        self.defaults.append((".".join(self.scope + [node.name]), values, kw))
        args.defaults = [ast.Constant(value="<default>") if _literal(d) else d for d in args.defaults]
        args.kw_defaults = [d if d is None or not _literal(d) else ast.Constant(value="<default>")
                            for d in args.kw_defaults]
        # Decorators, annotations and non-literal defaults run in the enclosing scope.
        node.decorator_list = [self.visit(d) for d in node.decorator_list]
        node.body = [ast.Pass()]
        return node

    visit_FunctionDef = _function
    visit_AsyncFunctionDef = _function

    def visit_Lambda(self, node):
        node.body = ast.Constant(value="<lambda>")
        return node


class _Opaque:
    def __init__(self, text):
        self.text = text

    def __eq__(self, other):
        return isinstance(other, _Opaque) and other.text == self.text

    def __hash__(self):
        return hash(self.text)


def _literal(node):
    try:
        ast.literal_eval(node)
        return True
    except Exception:
        return False


def _const_key(value):
    if isinstance(value, types.CodeType):
        return ("<code>", value.co_qualname)
    if isinstance(value, (tuple, frozenset)):
        items = tuple(_const_key(v) for v in value)
        return (type(value).__name__, tuple(sorted(items, key=repr)) if isinstance(value, frozenset) else items)
    return (type(value).__name__, repr(value))


def _own(code, skip_doc):
    consts = code.co_consts[1:] if skip_doc else code.co_consts
    return (code.co_code, tuple(map(_const_key, consts)), code.co_names, code.co_varnames,
            code.co_freevars, code.co_cellvars, code.co_argcount, code.co_posonlyargcount,
            code.co_kwonlyargcount, code.co_flags, code.co_exceptiontable, code.co_nlocals)


def _children(code):
    return [c for c in code.co_consts if isinstance(c, types.CodeType)]


def analyze(path, old_text, new_text, filename):
    """Return dict(changed=[keys], swaps=[(key, new_code)], docs=[...], defaults=[...])."""
    try:
        old_tree = ast.parse(old_text)
        new_tree = ast.parse(new_text)
    except SyntaxError as e:
        raise Unsupported("syntax") from e
    a, b = _Normalize(), _Normalize()
    try:
        old_norm = ast.dump(a.visit(old_tree))
        new_norm = ast.dump(b.visit(new_tree))
    except Exception as e:
        raise Unsupported("normalize") from e
    if old_norm != new_norm:
        raise Unsupported("structure")
    docs = [(kind, name, doc) for (kind, name, doc), (_, _, old) in zip(b.docs, a.docs) if doc != old]
    defaults = []
    for (name, values, kw), (_, old_values, old_kw) in zip(b.defaults, a.defaults):
        if values != old_values or kw != old_kw:
            if any(isinstance(v, _Opaque) for v in values + kw):
                raise Unsupported("default")
            defaults.append((name, values, kw))
    old_code = compile(old_text, filename, "exec", dont_inherit=True)
    new_code = compile(new_text, filename, "exec", dont_inherit=True)
    function_docs = {name for kind, name, _ in docs if kind == "function"}
    changed, swaps = [], []

    def walk(old, new, chain):
        if old.co_qualname != new.co_qualname:
            raise Unsupported("qualname")
        is_function = bool(old.co_flags & NEWLOCALS)
        key = (path, old.co_qualname, old.co_firstlineno)
        here = chain + [(key, new)] if is_function else []
        skip_doc = old.co_qualname in function_docs
        if is_function and _own(old, skip_doc) != _own(new, skip_doc):
            changed.append(key)
            swaps.extend(here)
        elif is_function and skip_doc:
            swaps.append((key, new))
        if is_function and len(old.co_freevars) != len(new.co_freevars):
            raise Unsupported("closure")
        oc, nc = _children(old), _children(new)
        if len(oc) != len(nc):
            raise Unsupported("children")
        for o, n in zip(oc, nc):
            walk(o, n, here)

    walk(old_code, new_code, [])
    unique = {}
    for key, code in swaps:
        unique[key] = code
    return {"changed": changed, "swaps": list(unique.items()), "docs": docs, "defaults": defaults}
