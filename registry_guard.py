"""Fall back when edits can change late dispatch or import resolution."""
import ast
from collections import deque
from functools import lru_cache
import re

from impact import UnknownChange, patched, split_diff, syntax, syntax_tree


DISPATCH = re.compile(r"(?:[A-Z_]+_)?(?:TRANSFORMS|FUNCTIONS|PARSERS|TYPE_MAPPING|HANDLERS|REGISTRY)$")
WORDS = re.compile(r"\b(?:[A-Z_]+_)?(?:TRANSFORMS|FUNCTIONS|PARSERS|TYPE_MAPPING|HANDLERS|REGISTRY)\b"
                   r"|\b(?:classes|_classes|import_module|__import__)\b")
ROUTERS = {"import_module", "__import__"}
ROUTER_WORDS = re.compile(r"\b(?:import_module|__import__)\b")
FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)
SCOPES = (*FUNCTIONS, ast.ClassDef)


def name(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def dispatch(node):
    if isinstance(node, ast.Subscript):
        return dispatch(node.value)
    if isinstance(node, (ast.Name, ast.Attribute)):
        key = name(node)
        return key in ("classes", "_classes") or bool(DISPATCH.fullmatch(key))
    return False


@lru_cache(maxsize=72)
def bindings(path, source):
    tree = syntax_tree(path, source)
    state = []
    lines = source.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    pending = deque([tree])
    while pending:
        item = pending.popleft()
        scope = None
        if isinstance(item, SCOPES):
            start = min((node.lineno for node in item.decorator_list), default=item.lineno)
            scope = "\n".join(lines[start - 1:item.end_lineno])
            if scope.isascii() and not WORDS.search(scope):
                continue
        pending.extend(ast.iter_child_nodes(item))
        if isinstance(item, (ast.Assign, ast.Delete)):
            if any(dispatch(target) for target in item.targets):
                state.append(ast.dump(item))
        elif isinstance(item, (ast.AnnAssign, ast.AugAssign)):
            if dispatch(item.target):
                state.append(ast.dump(item))
        elif isinstance(item, ast.Call):
            if (name(item.func) in ROUTERS or
                    isinstance(item.func, ast.Attribute) and item.func.attr in {"update", "clear", "pop", "popitem", "setdefault"}
                    and dispatch(item.func.value)):
                state.append(ast.dump(item))
        elif isinstance(item, FUNCTIONS):
            if scope.isascii() and not ROUTER_WORDS.search(scope):
                continue
            # Record the routing body too. A changed local can affect which
            # module is loaded even when the import call text is identical.
            if any(isinstance(call, ast.Call) and name(call.func) in ROUTERS
                   and (not call.args or not isinstance(call.args[0], ast.Constant))
                   for call in ast.walk(item)):
                state.append(ast.dump(item))
    return tuple(state)


@lru_cache(maxsize=8)
def changes_routing(impact, text):
    try:
        new_routing = WORDS.search(text) is not None
        for path, hunks in split_diff(text).items():
            if not path.startswith("sqlglot/") or not path.endswith(".py"):
                continue
            clean = (impact.root / path).read_text()
            if new_routing or WORDS.search(clean):
                revised = patched(clean, hunks)
                if bindings(path, clean) != bindings(path, revised):
                    # Let ordinary selection handle invalid Python.
                    syntax(path, revised)
                    return True
    except (OSError, UnknownChange, UnicodeError, SyntaxError, ValueError, RecursionError):
        # Leave other format and syntax decisions to the ordinary selector.
        return False
    return False
