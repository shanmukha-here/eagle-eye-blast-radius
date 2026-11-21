"""Conservative refinements using execution evidence from the unmodified source."""

import ast
import functools
import io
import subprocess
import sys
import tokenize
from pathlib import Path


def dump(node):
    return ast.dump(node) if isinstance(node, ast.AST) else node


def bindings(code):
    return (
        code.co_varnames,
        code.co_cellvars,
        code.co_freevars,
        code.co_flags,
        tuple(bindings(c) for c in code.co_consts if isinstance(c, type(code))),
    )


def encoding(source):
    return tokenize.detect_encoding(io.BytesIO(source.encode("utf-8")).readline)[0]


def regions(old, new):
    """Get entry regions for changed statements. An insertion needs a wider scope."""
    if isinstance(old, ast.expr):
        return [(old.lineno, old.end_lineno)]
    spans = []

    def compare(left, right):
        if len(left) != len(right):
            return False
        for a, b in zip(left, right):
            if dump(a) == dump(b):
                continue
            if isinstance(a, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                return False
            if type(a) is type(b) and isinstance(a, (ast.If, ast.While)):
                if dump(a.test) != dump(b.test):
                    # An edit to a later operand can change short-circuit behavior
                    # for tests that only executed the start of the predicate.
                    spans.append((a.lineno, a.test.end_lineno))
                if not (compare(a.body, b.body) and compare(a.orelse, b.orelse)):
                    return False
            else:
                spans.append((a.lineno, a.end_lineno))
        return True

    return spans if compare(old.body, new.body) else []


def literal_edit(left, right):
    """Same expression topology, operators, dictionary keys and literal types."""
    if type(left) is not type(right):
        return False
    if isinstance(left, ast.Constant):
        return type(left.value) is type(right.value) and (
            left.value == right.value or type(left.value) in (bool, int, float, str)
        )
    if isinstance(left, (ast.Lambda, ast.comprehension)):
        return dump(left) == dump(right)
    if isinstance(left, ast.Dict) and [dump(x) for x in left.keys] != [
        dump(x) for x in right.keys
    ]:
        return False
    if isinstance(left, ast.AST):
        return all(
            literal_edit(getattr(left, k), getattr(right, k)) for k in left._fields
        )
    if isinstance(left, list):
        return len(left) == len(right) and all(
            literal_edit(a, b) for a, b in zip(left, right)
        )
    return left == right


def changed_fields(old_source, new_source):
    old, new = ast.parse(old_source), ast.parse(new_source)
    changes = []

    def walk(a, b, scope, in_class=False):
        if type(a) is not type(b):
            return False
        if isinstance(a, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return dump(a) == dump(b)
        if isinstance(a, ast.ClassDef):
            scope = (*scope, a.name)
        if in_class and isinstance(a, (ast.Assign, ast.AnnAssign)):
            targets = a.targets if isinstance(a, ast.Assign) else [a.target]
            if (
                len(targets) == 1
                and isinstance(targets[0], ast.Name)
                and a.value is not None
                and b.value is not None
                and dump(a.value) != dump(b.value)
                and literal_edit(a.value, b.value)
            ):
                changes.append((".".join(scope), targets[0].id))
                b.value = a.value
        if isinstance(a, ast.AST):
            return all(
                walk(
                    getattr(a, k),
                    getattr(b, k),
                    scope,
                    isinstance(a, ast.ClassDef) and k == "body",
                )
                for k in a._fields
            )
        if isinstance(a, list):
            return len(a) == len(b) and all(
                walk(x, y, scope, in_class) for x, y in zip(a, b)
            )
        return a == b

    return changes if walk(old, new, ()) else []


class Details:
    def __init__(self, data, ids, root, access=None):
        self.access = access
        pool = [int(v, 16) for v in data["bits"]]
        self.lines = {
            p: {int(k): pool[i] for k, i in rows.items()}
            for p, rows in data["lines"].items()
        }
        self.positions = pool[data["positions"]]
        self.operand_types = {
            k: {t: pool[i] for t, i in rec.items()}
            for k, rec in data.get("operands", {}).items()
        }
        self.dialects = {p: pool[i] for p, i in data["dialects"].items()}
        self.types = {key: pool[i] for key, i in data.get("types", {}).items()}
        self.references = {
            key: pool[i] for key, i in data.get("references", {}).items()
        }
        self.full = (1 << len(ids)) - 1
        self.ids = ids
        self.imports = {p: pool[i] for p, i in data.get("imports", {}).items()}
        self.test_files = {}
        for i, node in enumerate(ids):
            filename = node.split("::", 1)[0]
            self.test_files[filename] = self.test_files.get(filename, 0) | (1 << i)
        self.root = Path(root).resolve()

    def equivalent(self, path, source, modified, readers, fallback):
        if encoding(source) != encoding(modified):
            return self.full
        return readers | self.positions if path in self.lines else fallback

    def statements(self, path, old, new, wide):
        spans = regions(old, new)
        if not spans:
            return wide
        masks = self.lines.get(path, {})
        selection = 0
        for start, end in spans:
            mask = 0
            for line in range(start, end + 1):
                mask |= masks.get(line, 0)
            if not mask:
                return wide
            selection |= mask
        # A lambda and its enclosing class initializer can share a source line.
        # For ordinary function bodies, keep independent line observations even
        # if a worker or fixture ran in a different context in the call trace.
        return selection & wide if isinstance(old, ast.expr) else selection

    def comparison(self, path, old, new, wide):
        """Prove equivalence only for contexts with complete, safe operand types."""
        before = [n for n in ast.walk(old) if isinstance(n, ast.Compare)]
        after = [n for n in ast.walk(new) if isinstance(n, ast.Compare)]
        if len(before) != len(after):
            return wide
        changed = [(a, b) for a, b in zip(before, after) if dump(a) != dump(b)]
        if len(changed) != 1:
            return wide
        a, b = changed[0]
        if len(a.ops) != 1 or len(b.ops) != 1:
            return wide
        ops = {type(a.ops[0]), type(b.ops[0])}
        if ops not in ({ast.Is, ast.Eq}, {ast.IsNot, ast.NotEq}):
            return wide
        original = b.ops
        b.ops = a.ops
        try:
            if dump(old) != dump(new):
                return wide
        finally:
            b.ops = original
        operands = (a.left, a.comparators[0])
        observed = [
            self.operand_types.get(f"{path}:{a.lineno}:{a.col_offset}:{i}", {})
            for i in range(2)
        ]
        for i, expr in enumerate(operands):
            if isinstance(expr, ast.Constant) and expr.value is None:
                good = bad = 0
                for t, mask in observed[1 - i].items():
                    if t.startswith(("builtin:", "enum:")) or t == "identity":
                        good |= mask
                    else:
                        bad |= mask
                return wide & ~(good & ~bad)
        # Equality and identity coincide for members of a single verified enum.
        good = 0
        for kind in observed[0].keys() & observed[1].keys():
            if not kind.startswith("enum:"):
                continue
            common = observed[0][kind] & observed[1][kind]
            for side in observed:
                for t, mask in side.items():
                    if t != kind:
                        common &= ~mask
            good |= common
        return wide & ~good

    def test_body(self, path, base_tree, tree):
        outer, scopes = base_tree
        after, changed = tree
        if outer != after or scopes.keys() != changed.keys():
            return self.full
        external = self.imports.get(path, self.full) & ~self.test_files.get(path, 0)
        selection = 0
        for key, (_, last, body, old_body) in scopes.items():
            if body == changed[key][2]:
                continue
            if isinstance(old_body, ast.Module) and ast.get_docstring(
                old_body, clean=False
            ) != ast.get_docstring(changed[key][3], clean=False):
                return self.full
            if not key[-2].startswith("test_"):
                return self.full
            matched = sum(
                1 << i
                for i, item in enumerate(self.ids)
                if item.split("::")[-1].split("[")[0] == key[-2]
            )
            if not matched:
                return self.full
            selection |= matched
            # Executing the def line at collection creates the method; its body
            # executes only under the calling test or fixture.
            for ln in range(scopes[key][3].body[0].lineno, last + 1):
                selection |= self.lines.get(path, {}).get(ln, 0)
        return selection | external if selection else self.full

    @functools.lru_cache(maxsize=96)
    def import_valid(self, path, modified):
        module = path[:-3].replace("/", ".")
        helper = Path(__file__).with_name("import_check.py")
        try:
            result = subprocess.run(
                [sys.executable, "-IBS", str(helper), str(self.root), module],
                input=modified.encode(),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=1.5,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return result.returncode == 0

    def fields(self, path, source, modified, readers, visitors):
        fields = changed_fields(source, modified)
        if not fields:
            return self.full
        selected = readers
        for owner, field in fields:
            identifier = f"{path}:{owner}"
            if path.startswith("sqlglot/expressions/") and field == "arg_types":
                if identifier not in self.types or identifier not in self.references:
                    return self.full
                selected |= self.types[identifier] | self.references[identifier]
            elif path in self.dialects and field.isupper():
                wide = self.dialects[path] | visitors | self.references.get(identifier, 0)
                seen = self.access.scalar_users(path, owner, field, source) if self.access else None
                selected |= wide if seen is None or seen == self.full else seen
            else:
                return self.full
        if selected == self.full or not self.import_valid(path, modified):
            return self.full
        return selected
