"""Fall back when edits can change late dispatch or import resolution."""
import ast
from collections import deque
from functools import lru_cache
import re

from impact import UnknownChange, patched, split_diff, syntax


DISPATCH = re.compile(r"(?:[A-Z_]+_)?(?:TRANSFORMS|FUNCTIONS|PARSERS|TYPE_MAPPING|HANDLERS|REGISTRY)$")
WORDS = re.compile(r"\b(?:[A-Z_]+_)?(?:TRANSFORMS|FUNCTIONS|PARSERS|TYPE_MAPPING|HANDLERS|REGISTRY)\b"
                   r"|\b(?:classes|_classes|import_module|__import__)\b")
ROUTERS = {"import_module", "__import__"}
MUTATORS = {"update", "clear", "pop", "popitem", "setdefault"}
FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)


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
def routing_nodes(path, source):
    tree = syntax(path, source)[0]
    seen = set()
    queue = deque([(tree, ())])
    position = 0
    while queue:
        item, functions = queue.popleft()
        if isinstance(item, FUNCTIONS):
            functions += ((position, item),)
        if isinstance(item, (ast.Assign, ast.Delete)):
            if any(dispatch(target) for target in item.targets):
                seen.add((position, item))
        elif isinstance(item, (ast.AnnAssign, ast.AugAssign)):
            if dispatch(item.target):
                seen.add((position, item))
        elif isinstance(item, ast.Call):
            method = name(item.func)
            if method in ROUTERS:
                seen.add((position, item))
                # A local in any enclosing function can determine the target.
                if not item.args or not isinstance(item.args[0], ast.Constant):
                    seen.update(functions)
            elif method in MUTATORS and isinstance(item.func, ast.Attribute) and dispatch(item.func.value):
                seen.add((position, item))
        queue.extend((child, functions) for child in ast.iter_child_nodes(item))
        position += 1
    return tuple(item for _, item in sorted(seen, key=lambda record: record[0]))


def bindings(path, source):
    return tuple(map(ast.dump, routing_nodes(path, source)))


def equivalent(left, right):
    """Compare parsed AST values with the same distinctions as ast.dump."""
    if left is right:
        return True
    if type(left) is not type(right):
        return False
    if isinstance(left, ast.AST):
        return all(equivalent(value, getattr(right, field)) for field, value in ast.iter_fields(left))
    if isinstance(left, list):
        return len(left) == len(right) and all(equivalent(a, b) for a, b in zip(left, right))
    return repr(left) == repr(right)


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
                before, after = routing_nodes(path, clean), routing_nodes(path, revised)
                if len(before) != len(after) or not all(equivalent(a, b) for a, b in zip(before, after)):
                    return True
    except (OSError, UnknownChange, UnicodeError, SyntaxError, ValueError, RecursionError):
        # Leave other format and syntax decisions to the ordinary selector.
        return False
    return False
