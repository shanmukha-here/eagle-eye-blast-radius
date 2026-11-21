"""Use clean receiver and import observations for isolated class declarations."""
import ast
from difflib import SequenceMatcher

from impact import UnknownChange, patched, split_diff, syntax
from observation_file import ObservationFile


METHODS = (ast.FunctionDef, ast.AsyncFunctionDef)


def classes(body, parent=""):
    for node in body:
        if isinstance(node, ast.ClassDef) and not node.decorator_list and not node.keywords:
            qualified = parent + node.name
            yield qualified, node
            yield from classes(node.body, qualified + ".")


def simple_literal(value):
    return isinstance(value, ast.Constant) and type(value.value) in (str, int, bool, float, type(None))


def plain_method(node):
    if not isinstance(node, METHODS) or node.decorator_list or node.name.startswith("_"):
        return False
    args = node.args
    params = args.posonlyargs + args.args + args.kwonlyargs
    params += [v for v in (args.vararg, args.kwarg) if v is not None]
    return not (node.returns or args.defaults or any(args.kw_defaults) or any(p.annotation for p in params))


def changed_member(left, right):
    if not left and len(right) == 1 and plain_method(right[0]):
        return right[0].name
    if not right and len(left) == 1 and plain_method(left[0]):
        return left[0].name
    if len(left) != 1 or len(right) != 1:
        return None
    a, b = left[0], right[0]
    if isinstance(a, ast.Assign) and isinstance(b, ast.Assign):
        if (len(a.targets) == len(b.targets) == 1 and isinstance(a.targets[0], ast.Name)
                and ast.dump(a.targets[0]) == ast.dump(b.targets[0])
                and not a.targets[0].id.startswith("_") and simple_literal(a.value) and simple_literal(b.value)):
            return a.targets[0].id
    if (isinstance(a, METHODS) and type(a) is type(b) and a.name == b.name
            and not a.name.startswith("_") and not a.decorator_list and not b.decorator_list):
        old = a.args.defaults + a.args.kw_defaults
        new = b.args.defaults + b.args.kw_defaults
        if not old or len(old) != len(new) or any(v is not None and not simple_literal(v) for v in old + new):
            return None
        defaults, kwargs = b.args.defaults, b.args.kw_defaults
        try:
            b.args.defaults, b.args.kw_defaults = a.args.defaults, a.args.kw_defaults
            if ast.dump(a) == ast.dump(b):
                return a.name
        finally:
            b.args.defaults, b.args.kw_defaults = defaults, kwargs
    return None


def declaration(before, after):
    """Require that precisely one member accounts for the entire AST difference."""
    new_classes = dict(classes(after.body))
    original = ast.dump(before)
    # Try the deepest class first so a nested member diff is not attributed
    # to a containing class's replacement statement.
    for owner, previous in reversed(list(classes(before.body))):
        current = new_classes.get(owner)
        if current is None or ast.dump(previous) == ast.dump(current):
            continue
        blocks = SequenceMatcher(None, list(map(ast.dump, previous.body)), list(map(ast.dump, current.body)),
                                 autojunk=False).get_opcodes()
        changes = [(a, b, c, d) for tag, a, b, c, d in blocks if tag != "equal"]
        if len(changes) != 1:
            continue
        a, b, c, d = changes[0]
        member = changed_member(previous.body[a:b], current.body[c:d])
        if member is None:
            continue
        body = current.body
        try:
            current.body = body[:c] + previous.body[a:b] + body[d:]
            if ast.dump(after) == original:
                return owner, member
        finally:
            current.body = body
    return None


class ReceiverEdits:
    def __init__(self, filename, impact):
        self.index = ObservationFile(filename) if filename.is_file() else None
        self.impact = impact

    def bits(self, key):
        rows = self.index.get(key)
        return rows[0][1] if rows else 0

    def select(self, patch):
        if self.index is None:
            return None
        try:
            edits = split_diff(patch)
            if len(edits) != 1:
                return None
            path, hunks = next(iter(edits.items()))
            if not path.startswith("sqlglot/") or not path.endswith(".py") or path.endswith("/__init__.py"):
                return None
            source, _, _ = self.impact.base(path)
            modified = patched(source, hunks)
            before, after = syntax(path, source)[0], syntax(path, modified)[0]
            changed = declaration(before, after)
            if changed is None:
                return None
            owner, member = changed
            qualified = path[:-3].replace("/", ".") + ":" + owner
            if not self.bits("plain:" + qualified):
                return None
            details = self.impact.details
            rows = self.impact.files.get(path, {})
            mask = (self.bits("d:" + path + ":" + owner) |
                    self.bits("r:" + qualified + ":" + member) |
                    self.bits("r:" + qualified + ":*") |
                    details.imports.get(path, 0) | details.dialects.get(path, 0) | rows.get(0, 0) |
                    details.references.get(path + ":" + owner, 0) | details.types.get(path + ":" + owner, 0) |
                    details.flow.opaque | getattr(details.flow, "reflect", 0))
            if mask == self.impact.full or not details.import_valid(path, modified):
                return None
            return mask
        except (OSError, UnknownChange, UnicodeError, SyntaxError, ValueError, RecursionError):
            return None
