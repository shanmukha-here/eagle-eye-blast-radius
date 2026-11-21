"""Contracts for replaying clean SQL assertions in isolation."""
import ast
import inspect
import textwrap
import unittest


HELPERS = {"validate_identity", "validate_transpile", "validate_all", "validate"}


def literals(value, depth=0):
    if depth > 10:
        return False
    if type(value) in (str, int, float, bool, type(None)):
        return True
    if type(value) in (tuple, list):
        return len(value) <= 200 and all(literals(item, depth + 1) for item in value)
    return (type(value) is dict and len(value) <= 200 and
            all(type(key) is str and literals(item, depth + 1) for key, item in value.items()))


def safe_value(node, names):
    if isinstance(node, ast.Constant):
        return literals(node.value)
    if isinstance(node, ast.Name):
        return node.id in names
    if isinstance(node, (ast.List, ast.Tuple)):
        return all(safe_value(item, names) for item in node.elts)
    if isinstance(node, ast.Dict):
        return all(key is not None and safe_value(key, names) and safe_value(value, names)
                   for key, value in zip(node.keys, node.values, strict=True))
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        return isinstance(node.operand, ast.Constant) and type(node.operand.value) in (int, float)
    return False


def bindings(node):
    if isinstance(node, ast.Name) and node.id != "self":
        return {node.id}
    if isinstance(node, (ast.Tuple, ast.List)):
        parts = [bindings(item) for item in node.elts]
        if all(part is not None for part in parts):
            return set().union(*parts)
    return None


def direct_call(node, methods, names):
    return (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name) and node.func.value.id == "self"
            and node.func.attr in methods and all(safe_value(arg, names) for arg in node.args)
            and all(arg.arg is not None and safe_value(arg.value, names) for arg in node.keywords))



SQL_FUNCTIONS = {"parse", "parse_one", "transpile", "optimize"}
SQL_ERRORS = {"ParseError", "TokenError", "UnsupportedError", "OptimizeError", "ValueError"}


def sql_name(node, names):
    return (isinstance(node, ast.Name) and node.id in names or
            isinstance(node, ast.Attribute) and node.attr in names and
            isinstance(node.value, ast.Name) and node.value.id == "sqlglot")


def sql_expression(node, names):
    if safe_value(node, names):
        return True
    if isinstance(node, ast.Subscript):
        return sql_expression(node.value, names) and safe_value(node.slice, names)
    if not isinstance(node, ast.Call):
        return False
    function = node.func
    eligible = sql_name(function, SQL_FUNCTIONS)
    if isinstance(function, ast.Attribute) and function.attr == "sql":
        eligible = sql_expression(function.value, names) and not safe_value(function.value, names)
    return (eligible and all(safe_value(arg, names) for arg in node.args) and
            all(arg.arg is not None and safe_value(arg.value, names) for arg in node.keywords))


def sql_assertion(node, names):
    if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and
            isinstance(node.func.value, ast.Name) and node.func.value.id == "self"):
        return False
    if node.func.attr == "assertEqual" and len(node.args) == 2 and not node.keywords:
        return (all(sql_expression(arg, names) for arg in node.args) and
                any(not safe_value(arg, names) for arg in node.args))
    if node.func.attr == "assertRaises" and len(node.args) >= 3:
        kind, target, *args = node.args
        return (sql_name(kind, SQL_ERRORS) and sql_name(target, SQL_FUNCTIONS) and
                all(safe_value(arg, names) for arg in args) and
                all(arg.arg and safe_value(arg.value, names) for arg in node.keywords))
    return False


def uses_local_names(node):
    names = {child.id for child in ast.walk(node) if isinstance(child, ast.Name)}
    return names - SQL_FUNCTIONS - SQL_ERRORS - {"sqlglot", "self"}

def safe_body(body, methods, names=frozenset()):
    for stmt in body:
        if isinstance(stmt, ast.Expr):
            if isinstance(stmt.value, ast.Constant) and isinstance(stmt.value.value, str):
                continue
            if direct_call(stmt.value, methods, names) or sql_assertion(stmt.value, names):
                continue
        if isinstance(stmt, ast.For) and not stmt.orelse and safe_value(stmt.iter, names):
            bound = bindings(stmt.target)
            if bound and safe_body(stmt.body, methods, names | bound):
                continue
        if isinstance(stmt, ast.With) and all(item.optional_vars is None and
                direct_call(item.context_expr, {"subTest"}, names) for item in stmt.items):
            if safe_body(stmt.body, methods, names):
                continue
        if isinstance(stmt, ast.Pass):
            continue
        return False
    return True


def admissible(cls, method, owners):
    if not issubclass(cls, unittest.TestCase):
        return False
    for name in ("__init__", "setUp", "tearDown", "setUpClass", "tearDownClass",
                 "run", "subTest", "assertEqual", "assertRaises", "__getattribute__"):
        actual = getattr(cls, name)
        baseline = getattr(unittest.TestCase, name)
        if getattr(actual, "__func__", actual) is not getattr(baseline, "__func__", baseline):
            return False
    methods = set()
    for owner, supported in owners.items():
        if issubclass(cls, owner):
            methods.update(name for name in supported if getattr(cls, name) is getattr(owner, name))
            if hasattr(owner, "parse_one") and cls.parse_one is not owner.parse_one:
                return False
    if type(getattr(cls, "dialect", None)) not in (str, type(None)):
        return False
    try:
        fn, = ast.parse(textwrap.dedent(inspect.getsource(getattr(cls, method)))).body
        return isinstance(fn, ast.FunctionDef) and not fn.decorator_list and safe_body(fn.body, methods)
    except (OSError, TypeError, SyntaxError, ValueError):
        return False
