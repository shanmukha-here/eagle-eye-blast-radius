"""Map a semantic diff to the test contexts that can see it."""

import ast
import json
import zlib
from functools import lru_cache

from observation_file import ObservationFile
from impact import patched, split_diff, syntax, UnknownChange
from refine import changed_fields
from value_observations import COMPARE_BITS, SCALARS, input_mask, key, replacements


def positional_defaults(args):
    positional = args.posonlyargs + args.args
    return zip(positional[len(positional) - len(args.defaults):], args.defaults)


def default_changes(old, new):
    """Require unchanged signatures, decorators, bodies and module statements."""
    changes = []
    restore = []
    if len(old.body) != len(new.body):
        return []
    for a, b in zip(old.body, new.body):
        if not (type(a) is type(b) and isinstance(a, (ast.FunctionDef, ast.AsyncFunctionDef))
                and not a.decorator_list and not b.decorator_list):
            continue
        old_defaults = list(positional_defaults(a.args)) + list(zip(a.args.kwonlyargs, a.args.kw_defaults))
        new_defaults = list(positional_defaults(b.args)) + list(zip(b.args.kwonlyargs, b.args.kw_defaults))
        if len(old_defaults) != len(new_defaults):
            break
        for (name, ref), (_, modified) in zip(old_defaults, new_defaults):
            if isinstance(ref, ast.Constant) and isinstance(modified, ast.Constant):
                if type(ref.value) is type(modified.value) and type(ref.value) in SCALARS and ref.value != modified.value:
                    changes.append((a.lineno, a.name, name.arg))
                    restore.append((modified, ref.value, modified.value))
    try:
        for node, value, _ in restore:
            node.value = value
        return changes if ast.dump(old) == ast.dump(new) else []
    finally:
        for node, _, value in restore:
            node.value = value


def import_activation(old, new, path, ready):
    """An annotation-only import may read an existing binding without running module code."""
    if len(old.body) != len(new.body):
        return False
    aliases = {name.asname or name.name for statement in old.body if isinstance(statement, ast.Import)
               for name in statement.names if name.name == "typing"}
    if not any(isinstance(s, ast.ImportFrom) and s.module == "__future__"
               and any(a.name == "annotations" for a in s.names) for s in old.body):
        return False
    changed = [(a, b) for a, b in zip(old.body, new.body) if ast.dump(a) != ast.dump(b)]
    if len(changed) != 1:
        return False
    before, after = changed[0]
    if not (isinstance(before, ast.If) and isinstance(after, ast.If) and not before.orelse
            and not after.orelse and isinstance(before.test, ast.Attribute)
            and isinstance(before.test.value, ast.Name) and before.test.value.id in aliases
            and before.test.attr == "TYPE_CHECKING" and isinstance(after.test, ast.Constant)
            and after.test.value is True and f"{path}:{before.lineno}" in ready
            and [ast.dump(n) for n in before.body] == [ast.dump(n) for n in after.body]):
        return False
    if any(not isinstance(stmt, ast.ImportFrom) or stmt.level or not stmt.module
           or any(a.name == "*" for a in stmt.names) for stmt in before.body):
        return False
    bound = {a.asname or a.name for stmt in before.body for a in stmt.names}
    # Only annotations may refer to the new binding. The future import stores
    # them as strings, so they perform no reads at definition time.
    def runtime_names(node):
        if isinstance(node, ast.Name):
            yield node.id
        for field, value in ast.iter_fields(node):
            if field in ("annotation", "returns"):
                continue
            children = value if isinstance(value, list) else [value]
            for child in children:
                if isinstance(child, ast.AST):
                    yield from runtime_names(child)
    return not bound.intersection(runtime_names(old))


def comparison_changes(old, new):
    """Allow comparison changes that evaluate the same pair of operands."""
    original = [node for node in ast.walk(old) if isinstance(node, ast.Compare)]
    updated = [node for node in ast.walk(new) if isinstance(node, ast.Compare)]
    if len(original) != len(updated):
        return []
    edits = []
    for before, after in zip(original, updated):
        if len(before.ops) != 1 or len(after.ops) != 1:
            continue
        left, right = type(before.ops[0]), type(after.ops[0])
        if left is not right and left in COMPARE_BITS and right in COMPARE_BITS:
            edits.append((before, after, after.ops))
    if not edits:
        return []
    try:
        for before, after, _ in edits:
            after.ops = before.ops
        return [(before, COMPARE_BITS[type(before.ops[0])], COMPARE_BITS[type(ops[0])])
                for before, _, ops in edits] if ast.dump(old) == ast.dump(new) else []
    finally:
        for _, after, ops in edits:
            after.ops = ops


def filtered_fixture(text):
    # The pinned loader's raw comment filter runs before metadata parsing.
    return "\n".join(line for line in text.splitlines() if line and not line.startswith("--"))


class ObservationIndex:
    def __init__(self, impact, filename):
        self.impact = impact
        self.details = impact.details
        self.base = impact.root
        index = filename.with_name("values.index")
        self.index = ObservationFile(index) if index.is_file() else None
        if self.index is None:
            data = json.loads(zlib.decompress(filename.read_bytes()))
            self.defaults = {key: int(bits, 16) for key, bits in data["defaults"].items()}
            self.values = data["values"]
            eq = filename.with_name("values_eq.z")
            self.identities = json.loads(zlib.decompress(eq.read_bytes()))["values"]
        self.fixtures = {
            path: int(bits, 16) for path, bits in
            json.loads(filename.with_name("fixture_inputs.json").read_bytes()).items()
        }
        self.ready = set(json.loads(filename.with_name("ready_imports.json").read_bytes()))
        self.references = {}
        for key, bits in self.details.references.items():
            path, _, _ = key.partition(":")
            self.references[path] = self.references.get(path, 0) | bits

    @lru_cache(maxsize=96)
    def original(self, path):
        text = (self.base / path).read_text()
        return text, syntax(path, text)

    @lru_cache(maxsize=1024)
    def observed_values(self, site):
        if self.index is not None:
            return self.index.get("v" + site)
        return [(None if values is None else tuple(v for _, v in values), int(bits, 16))
                for values, bits in self.values.get(site, ())]

    def comparisons(self, site):
        if self.index is not None:
            return self.index.get("c" + site)
        return tuple((tuple(v for _, v in values), int(bits, 16))
                     for values, bits in self.identities.get(site, ()))

    def default(self, site):
        if self.index is not None:
            rows = self.index.get("d" + site)
            return rows[0][1] if rows else None
        return self.defaults.get(site)

    def edit(self, text):
        try:
            edits = split_diff(text)
            if len(edits) != 1:
                return None
            path, hunks = next(iter(edits.items()))
            if path in self.fixtures:
                source = (self.base / path).read_text()
                if filtered_fixture(source) == filtered_fixture(patched(source, hunks)):
                    return "fixture", self.fixtures[path]
                return None
            if not path.startswith("sqlglot/") or not path.endswith(".py") or path.endswith("/__init__.py"):
                return None
            if path not in self.details.lines:
                return None
            source, (old, (outer, original_scopes), _) = self.original(path)
            modified = patched(source, hunks)
            external = (self.impact.files.get(path, {}).get(0, 0) |
                        self.details.flow.opaque | self.details.positions)
            try:
                after, (revised_outer, revised_scopes), _ = syntax(path, modified)
            except SyntaxError:
                imports = self.details.imports.get(path, 0)
                dialects = self.details.dialects.get(path, 0)
                if not (imports or dialects):
                    return None
                affected = (external | imports | dialects | self.references.get(path, 0) |
                            self.impact.visitors.get(path, 0))
                return "syntax", affected
            bodies = []
            if outer == revised_outer and original_scopes.keys() == revised_scopes.keys():
                bodies = [(prior[3], revised_scopes[site][3])
                          for site, prior in original_scopes.items()
                          if prior[2] != revised_scopes[site][2]]
            comparisons = []
            for before_body, after_body in bodies:
                edits = comparison_changes(before_body, after_body)
                if not edits:
                    comparisons.clear()
                    break
                comparisons.extend(edits)
            if comparisons:
                affected = external
                for compare, left, right in comparisons:
                    records = self.comparisons(key(path, compare) + ":comparison")
                    if not records:
                        break
                    covered = 0
                    for (known, truth), bits in records:
                        covered |= bits
                        required = left | right
                        if known & required != required or bool(truth & left) != bool(truth & right):
                            affected |= bits
                    affected |= self.details.lines[path].get(compare.lineno, self.impact.full) & ~covered
                else:
                    return "comparison", affected
            scalar = []
            for before_body, after_body in bodies:
                edits = replacements(before_body, after_body, within_function=isinstance(before_body, ast.Module))
                if not edits:
                    scalar.clear()
                    break
                scalar.extend(edits)
            if scalar:
                affected = external
                for previous, revised, names, predicate in scalar:
                    values = self.observed_values(key(path, previous))
                    if not values:
                        return None
                    observed, changed = input_mask(values, names, previous, revised, predicate)
                    # Keep contexts whose independently collected line
                    # coverage has no matching input in this trace.
                    line = self.details.lines[path].get(previous.lineno, self.impact.full)
                    affected |= changed | (line & ~observed)
                return "scalar", affected

            if outer == revised_outer:
                return None
            declarations = default_changes(old, after)
            if declarations:
                affected = external
                for line, function, arg in declarations:
                    bits = self.default(f"{path}:{line}:{function}:{arg}")
                    if bits is None:
                        return None
                    affected |= bits
                # No new binding, definition execution or registry mutation.
                # Each changed value is an existing scalar literal.
                return "default", affected
            fields = changed_fields(source, modified)
            if fields and self.details.access:
                affected = external
                for owner, field in fields:
                    readers = self.details.access.scalar_users(path, owner, field, source)
                    if readers is None and self.details.observers:
                        readers = self.details.observers.users(path, owner, field, source, modified)
                    if readers is None:
                        return None
                    affected |= readers
                if affected != self.impact.full and self.details.import_valid(path, modified):
                    return "attribute", affected
            if import_activation(old, after, path, self.ready) and self.details.import_valid(path, modified):
                affected = (external | self.details.imports.get(path, 0) |
                            self.impact.visitors.get(path, 0) | self.references.get(path, 0))
                if affected != self.impact.full:
                    return "import", affected
        except (UnknownChange, OSError, ValueError, SyntaxError, RecursionError):
            return None
        return None
