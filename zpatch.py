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


def _scope(code):
    # Equal locals and flags: statements outside the edit read and bind names the same way.
    return (code.co_varnames, code.co_cellvars, code.co_freevars, code.co_flags, code.co_argcount,
            code.co_posonlyargcount, code.co_kwonlyargcount)


def _children(code):
    return [c for c in code.co_consts if isinstance(c, types.CodeType)]


def _members(tree):
    """{(scope, name): [def nodes]} for functions directly in module or class bodies, with
    the list holding each body's statements so a def can be taken out."""
    out = {}

    def visit(body, scope):
        for node in body:
            if isinstance(node, ast.ClassDef):
                visit(node.body, scope + [node.name])
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                out.setdefault((".".join(scope), node.name), []).append((node, body))

    visit(tree.body, [])
    return out


def _passthrough(node):
    """def N(self, *args, **kwargs): return super().N(*args, **kwargs)"""
    a = node.args
    if a.posonlyargs or a.kwonlyargs or a.defaults or len(a.args) != 1 or not a.vararg \
            or not a.kwarg or node.decorator_list or node.returns or len(node.body) != 1:
        return False
    ret = node.body[0]
    if not isinstance(ret, ast.Return) or not isinstance(ret.value, ast.Call):
        return False
    call = ret.value
    f = call.func
    return (isinstance(f, ast.Attribute) and f.attr == node.name and isinstance(f.value, ast.Call)
            and isinstance(f.value.func, ast.Name) and f.value.func.id == "super"
            and not f.value.args and not f.value.keywords and len(call.args) == 1
            and isinstance(call.args[0], ast.Starred) and isinstance(call.args[0].value, ast.Name)
            and call.args[0].value.id == a.vararg.arg and len(call.keywords) == 1
            and call.keywords[0].arg is None and isinstance(call.keywords[0].value, ast.Name)
            and call.keywords[0].value.id == a.kwarg.arg)


def _split_members(old_tree, new_tree):
    """Take out functions that exist on one side only; return (added, removed) as
    (scope, name, node) lists."""
    old_m, new_m = _members(old_tree), _members(new_tree)
    added, removed = [], []
    for key in set(old_m) | set(new_m):
        o, n = old_m.get(key, []), new_m.get(key, [])
        if len(o) == len(n):
            continue
        if o and n:
            raise Unsupported("member-count")
        for node, body in o or n:
            if isinstance(node, ast.AsyncFunctionDef) or node.decorator_list:
                raise Unsupported("member-kind")
            body.remove(node)
            (removed if o else added).append((key[0], key[1], node))
    return added, removed


def names(text, tree=None):
    """(identifiers, identifier strings, compared names, non-call references) of a source."""
    if tree is None:
        tree = ast.parse(text)
    ids, strings, compared, loads = set(), set(), set(), set()
    called = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            called.add(id(node.func))
        elif isinstance(node, ast.Compare):
            for side in [node.left] + node.comparators:
                if isinstance(side, ast.Attribute):
                    compared.add(side.attr)
                elif isinstance(side, ast.Name):
                    compared.add(side.id)
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            ids.add(node.id)
            if id(node) not in called:
                loads.add(node.id)
        elif isinstance(node, ast.Attribute):
            ids.add(node.attr)
            if id(node) not in called:
                loads.add(node.attr)
        elif isinstance(node, ast.alias):
            for part in node.name.split(".") + ([node.asname] if node.asname else []):
                ids.add(part)
                loads.add(part)
        elif isinstance(node, ast.keyword) and node.arg:
            ids.add(node.arg)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            ids.update(node.names)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            for part in node.value.replace(".", " ").split():
                if part.isidentifier():
                    strings.add(part)
    return sorted(ids), sorted(strings), sorted(compared), sorted(loads)


VALUE_STATEMENTS = (ast.Assign, ast.AnnAssign, ast.AugAssign)


def _bound(node):
    """Names a module or class assignment binds, or None if it binds anything else."""
    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
    out = []
    for t in targets:
        for n in ([t] if not isinstance(t, (ast.Tuple, ast.List)) else t.elts):
            if not isinstance(n, ast.Name):
                return None
            out.append(n.id)
    return out


def _value_edits(old_body, new_body, scope, out):
    """Collect (scope, names) of module and class assignments whose values differ between
    two normalized bodies; raise Unsupported for any other difference."""
    if len(old_body) != len(new_body):
        raise Unsupported("structure")
    for o, n in zip(old_body, new_body):
        if ast.dump(o) == ast.dump(n):
            continue
        if isinstance(o, ast.ClassDef) and isinstance(n, ast.ClassDef):
            head = lambda c: ast.dump(ast.ClassDef(name=c.name, bases=c.bases, keywords=c.keywords,  # noqa: E731
                                                   body=[], decorator_list=c.decorator_list,
                                                   type_params=getattr(c, "type_params", [])))
            if head(o) != head(n):
                raise Unsupported("structure")
            _value_edits(o.body, n.body, scope + [o.name], out)
            continue
        if type(o) is not type(n) or not isinstance(o, VALUE_STATEMENTS):
            raise Unsupported("structure")
        names = _bound(o)
        if names is None or names != _bound(n):
            raise Unsupported("structure")
        if isinstance(o, ast.AugAssign) and type(o.op) is not type(n.op):
            raise Unsupported("structure")
        out.append((".".join(scope), names))


def analyze(path, old_text, new_text, filename, values=False):
    """Return dict(changed=[keys], swaps=[(key, new_code)], docs=[...], defaults=[...]).

    With ``values``, module and class assignments may also differ; they are listed under
    "values" and the caller must compare the imported values."""
    try:
        old_tree = ast.parse(old_text)
        new_tree = ast.parse(new_text)
    except SyntaxError as e:
        raise Unsupported("syntax") from e
    added, removed = _split_members(old_tree, new_tree)
    member_names = None
    if added or removed:
        member_names = names(new_text, new_tree)
    a, b = _Normalize(), _Normalize()
    try:
        old_norm = ast.dump(a.visit(old_tree))
        new_norm = ast.dump(b.visit(new_tree))
    except Exception as e:
        raise Unsupported("normalize") from e
    edits = []
    if old_norm != new_norm:
        if not values:
            raise Unsupported("structure")
        _value_edits(old_tree.body, new_tree.body, [], edits)
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
    changed, swaps, scoped = [], [], []

    def walk(old, new, chain):
        if old.co_qualname != new.co_qualname:
            raise Unsupported("qualname")
        is_function = bool(old.co_flags & NEWLOCALS)
        key = (path, old.co_qualname, old.co_firstlineno)
        here = chain + [(key, new)] if is_function else []
        skip_doc = old.co_qualname in function_docs
        if is_function and _own(old, skip_doc) != _own(new, skip_doc):
            changed.append(key)
            if _scope(old) == _scope(new):
                scoped.append(key)
            swaps.extend(here)
        elif is_function and skip_doc:
            swaps.append((key, new))
        if is_function and len(old.co_freevars) != len(new.co_freevars):
            raise Unsupported("closure")
        oc = [c for c in _children(old) if c.co_qualname not in gone]
        nc = [c for c in _children(new) if c.co_qualname not in new_only]
        if len(oc) != len(nc):
            raise Unsupported("children")
        for o, n in zip(oc, nc):
            walk(o, n, here)

    gone = {(scope + "." if scope else "") + name for scope, name, _ in removed}
    new_only = {(scope + "." if scope else "") + name for scope, name, _ in added}
    walk(old_code, new_code, [])
    unique = {}
    for key, code in swaps:
        unique[key] = code
    return {"changed": changed, "swaps": list(unique.items()), "docs": docs, "defaults": defaults,
            "scoped": scoped, "old_text": old_text,
            "added": [(scope, name, _passthrough(node)) for scope, name, node in added],
            "removed": [(scope, name, (path, (scope + "." if scope else "") + name, node.lineno))
                        for scope, name, node in removed],
            "names": member_names, "values": edits}


def changed_lines(hunks):
    """Old-file line numbers (1-based) of replaced or deleted lines, and insertion points as
    (line before, line after) pairs, from same-file hunks (start, new_start, old, new)."""
    import difflib
    lines, inserts = set(), []
    for start, _, old, new in hunks:
        for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, old, new, autojunk=False).get_opcodes():
            if tag in ("replace", "delete"):
                lines.update(range(start + i1 + 1, start + i2 + 1))
            elif tag == "insert":
                inserts.append((start + i1, start + i1 + 1))
    return lines, inserts


def _statements(body):
    for node in body:
        yield node


def _innermost(body, line):
    """Innermost statement in ``body`` (recursively) whose span contains ``line``."""
    for node in body:
        if node.lineno <= line <= node.end_lineno:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                return node, None
            for field in ("body", "orelse", "finalbody"):
                inner = getattr(node, field, None)
                if inner:
                    found = _innermost(inner, line)
                    if found is not None:
                        return found
            for handler in getattr(node, "handlers", ()) or ():
                found = _innermost(handler.body, line)
                if found is not None:
                    return found
            for case in getattr(node, "cases", ()) or ():
                found = _innermost(case.body, line)
                if found is not None:
                    return found
            return node, node
    return None


HEADED = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith)


def _range(node, line):
    """Lines whose execution shows that control reached the edited part of ``node``. Headers
    of these statements evaluate an expression first, so they always report a line."""
    body = getattr(node, "body", None)
    if isinstance(node, HEADED) and body and line < body[0].lineno:
        return node.lineno, body[0].lineno - 1
    return node.lineno, node.end_lineno


def function_nodes(tree):
    """Map (qualname-free name, first line) to function and lambda nodes, as code objects key them."""
    out = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            first = min([node.lineno] + [d.lineno for d in node.decorator_list])
            out.setdefault((node.name, first), []).append(node)
        elif isinstance(node, ast.Lambda):
            out.setdefault(("<lambda>", node.lineno), []).append(node)
    return out


def line_ranges(tree, nodes, key, lines, inserts):
    """Line ranges inside the changed code object ``key`` that executions of the edit must
    touch, or None when the edit cannot be placed below the function level."""
    _, qualname, first = key
    found = nodes.get((qualname.rsplit(".", 1)[-1], first), [])
    if len(found) != 1:
        return None
    node = found[0]
    if isinstance(node, ast.Lambda):
        return [(node.lineno, node.end_lineno)]
    body = node.body
    start = body[0].lineno
    if body and _doc_index(body) == 0:
        body = body[1:]
        if not body:
            return None
        start = body[0].lineno
    nested = [(n.lineno, n.end_lineno) for n in ast.walk(node)
              if n is not node and isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef))]

    def inside_nested(line):
        return any(a <= line <= b for a, b in nested)

    ranges = []
    mine = [line for line in lines if node.lineno <= line <= node.end_lineno and not inside_nested(line)]
    points = [p for p in inserts if node.lineno <= p[0] <= node.end_lineno and not inside_nested(p[0])]
    if not mine and not points:
        return None
    for line in mine:
        if line < start:
            return None
        found = _innermost(body, line)
        if found is None:
            return None
        stmt, _ = found
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            return None
        ranges.append(_range(stmt, line))
    if points:
        # Control can reach an insertion point without running the statements around it.
        return None
    return ranges
