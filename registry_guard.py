"""Fall back when edits can change late dispatch or import resolution."""
import ast
from functools import lru_cache
import re

from impact import UnknownChange, patched, split_diff, syntax


DISPATCH = re.compile(r"(?:[A-Z_]+_)?(?:TRANSFORMS|FUNCTIONS|PARSERS|TYPE_MAPPING|HANDLERS|REGISTRY)$")
WORDS = re.compile(r"\b(?:[A-Z_]+_)?(?:TRANSFORMS|FUNCTIONS|PARSERS|TYPE_MAPPING|HANDLERS|REGISTRY)\b"
                   r"|\b(?:classes|_classes|import_module|__import__)\b")
ROUTERS = {"import_module", "__import__"}


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
    tree = syntax(path, source)[0]
    state = []
    for item in ast.walk(tree):
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
        elif isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
            # Record the routing body too. A changed local can affect which
            # module is loaded even when the import call text is identical.
            if any(isinstance(call, ast.Call) and name(call.func) in ROUTERS
                   and (not call.args or not isinstance(call.args[0], ast.Constant))
                   for call in ast.walk(item)):
                state.append(ast.dump(item))
    return tuple(state)


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
                    return True
    except (OSError, UnknownChange, UnicodeError, SyntaxError, ValueError, RecursionError):
        # Leave other format and syntax decisions to the ordinary selector.
        return False
    return False
