"""Compare a patch's scalar expressions against inputs observed on the pin."""

import ast
import operator

EXPRESSION_FIELDS = {
    ast.If: "test", ast.While: "test", ast.Return: "value", ast.Assert: "test", ast.IfExp: "test",
}
COMPARISONS = {
    ast.Eq: operator.eq, ast.NotEq: operator.ne, ast.Lt: operator.lt,
    ast.LtE: operator.le, ast.Gt: operator.gt, ast.GtE: operator.ge,
    ast.Is: operator.is_, ast.IsNot: operator.is_not,
}
SCALARS = (type(None), bool, int, str)


def inputs(expr):
    if expr is None:
        return None
    nodes = list(ast.walk(expr))
    if len(nodes) > 19 or not any(isinstance(n, ast.Name) for n in nodes):
        return None
    if any(
        type(n) not in (ast.Name, ast.Load, ast.Constant, ast.UnaryOp, ast.Not,
                        ast.BoolOp, ast.And, ast.Or, ast.Compare, ast.IfExp, *COMPARISONS)
        or isinstance(n, ast.Constant) and type(n.value) not in SCALARS
        or isinstance(n, ast.Compare) and len(n.ops) != 1
        for n in nodes
    ):
        return None
    return tuple(sorted({n.id for n in nodes if isinstance(n, ast.Name)}))


def evaluate(expr, values):
    if isinstance(expr, ast.Constant):
        return expr.value
    if isinstance(expr, ast.Name):
        return values[expr.id]
    if isinstance(expr, ast.UnaryOp):
        return not evaluate(expr.operand, values)
    if isinstance(expr, ast.BoolOp):
        for term in expr.values:
            value = evaluate(term, values)
            if bool(value) == isinstance(expr.op, ast.Or):
                return value
        return value
    if isinstance(expr, ast.IfExp):
        branch = expr.body if evaluate(expr.test, values) else expr.orelse
        return evaluate(branch, values)
    left = evaluate(expr.left, values)
    right = evaluate(expr.comparators[0], values)
    op = type(expr.ops[0])
    if op in (ast.Is, ast.IsNot) and type(left) not in SCALARS[:2] and type(right) not in SCALARS[:2]:
        raise ValueError("identity of untracked objects")
    return COMPARISONS[op](left, right)


def key(path, expr):
    return f"{path}:{expr.lineno}:{expr.col_offset}"


def fingerprint(node):
    return ast.dump(node) if isinstance(node, ast.AST) else node


def replacements(old, new):
    """Match scalar expression edits while requiring an identical surrounding tree."""
    changes = []

    def compare(a, b, owner=None):
        if type(a) is not type(b):
            return False
        if isinstance(a, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            owner = a
        if isinstance(a, ast.AST):
            field = EXPRESSION_FIELDS.get(type(a))
            if field and isinstance(owner, (ast.FunctionDef, ast.AsyncFunctionDef)):
                before, after = getattr(a, field), getattr(b, field)
                if fingerprint(before) != fingerprint(after):
                    names = inputs(before)
                    new_names = inputs(after)
                    if names is not None and new_names is not None and set(new_names) <= set(names):
                        changes.append((before, after, names, field == "test"))
                        return all(compare(getattr(a, attr), getattr(b, attr), owner)
                                   for attr in a._fields if attr != field)
            return all(compare(getattr(a, attr), getattr(b, attr), owner) for attr in a._fields)
        if isinstance(a, list):
            return len(a) == len(b) and all(compare(x, y, owner) for x, y in zip(a, b))
        return a == b

    return changes if compare(old, new) else []


def input_mask(records, names, before, after, predicate):
    """Return bits for inputs that differ or could not be compared."""
    observed = affected = 0
    for raw, bits in records:
        observed |= bits
        if raw is None or len(raw) != len(names):
            affected |= bits
            continue
        values = dict(zip(names, raw))
        try:
            a, b = evaluate(before, values), evaluate(after, values)
            equivalent = bool(a) == bool(b) if predicate else (
                type(a) in (bool, type(None)) and a is b
            )
            if not equivalent:
                affected |= bits
        except (KeyError, TypeError, ValueError, OverflowError):
            affected |= bits
    return observed, affected
