"""Fall back when edits can change late dispatch or import resolution."""
import ast
from functools import lru_cache
from hashlib import sha256
import re

from impact import UnknownChange, patched, split_diff, syntax
from precision_index import build


DISPATCH = re.compile(r"(?:[A-Z_]+_)?(?:TRANSFORMS|FUNCTIONS|PARSERS|TYPE_MAPPING|HANDLERS|REGISTRY)$")
WORDS = re.compile(r"\b(?:[A-Z_]+_)?(?:TRANSFORMS|FUNCTIONS|PARSERS|TYPE_MAPPING|HANDLERS|REGISTRY)\b"
                   r"|\b(?:classes|_classes|import_module|__import__)\b")
ROUTERS = {"import_module", "__import__"}
FINGERPRINT_WIDTH = 32


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
    nodes = list(ast.walk(tree))
    dynamic = [item for item in nodes if isinstance(item, ast.Call) and name(item.func) in ROUTERS
               and (not item.args or not isinstance(item.args[0], ast.Constant))]
    scopes = set()
    if dynamic:
        parents = {child: node for node in nodes for child in ast.iter_child_nodes(node)}
        for item in dynamic:
            while item in parents:
                item = parents[item]
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    scopes.add(item)
    state = []
    for item in nodes:
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
        elif item in scopes:
            # Keep each enclosing function because a local assignment can
            # change the module name while preserving the call expression.
            state.append(ast.dump(item))
    return tuple(state)


def fingerprint(state):
    return int.from_bytes(sha256(repr(state).encode()).digest(), "little")


@lru_cache(maxsize=8)
def changes_routing(impact, text):
    try:
        new_routing = WORDS.search(text) is not None
        for path, hunks in split_diff(text).items():
            if not path.startswith("sqlglot/") or not path.endswith(".py"):
                continue
            lookup = getattr(impact, "routing", None)
            saved = lookup.mask(path) if lookup is not None else 0
            if saved and not new_routing and not lookup.mask("words:" + path):
                continue
            clean = (impact.root / path).read_text()
            if saved or new_routing or WORDS.search(clean):
                revised = patched(clean, hunks)
                incoming = bindings(path, revised)
                if saved and saved != fingerprint(incoming) or not saved and bindings(path, clean) != incoming:
                    return True
    except (OSError, UnknownChange, UnicodeError, SyntaxError, ValueError, RecursionError):
        # Leave other format and syntax decisions to the ordinary selector.
        return False
    return False


if __name__ == "__main__":
    from pathlib import Path
    from sys import argv

    base, output = map(Path, argv[1:])
    states = {}
    for source in sorted((base / "sqlglot").rglob("*.py")):
        path = source.relative_to(base).as_posix()
        text = source.read_text()
        states[path] = fingerprint(bindings(path, text))
        if WORDS.search(text):
            states["words:" + path] = 1
    build(output, states, FINGERPRINT_WIDTH)
