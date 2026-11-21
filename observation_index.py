"""Map a semantic diff to the test contexts that can see it."""

import ast
import json
import zlib
from functools import lru_cache

from impact import patched, split_diff, UnknownChange
from value_observations import SCALARS, input_mask, key, replacements


def positional_defaults(args):
    positional = args.posonlyargs + args.args
    return zip(positional[len(positional) - len(args.defaults):], args.defaults)


def default_changes(old, new):
    """Require unchanged signatures, decorators, bodies and module statements."""
    changes = []
    if len(old.body) != len(new.body):
        return []
    for a, b in zip(old.body, new.body):
        if not (type(a) is type(b) and isinstance(a, (ast.FunctionDef, ast.AsyncFunctionDef))
                and not a.decorator_list and not b.decorator_list):
            continue
        old_defaults = list(positional_defaults(a.args)) + list(zip(a.args.kwonlyargs, a.args.kw_defaults))
        new_defaults = list(positional_defaults(b.args)) + list(zip(b.args.kwonlyargs, b.args.kw_defaults))
        if len(old_defaults) != len(new_defaults):
            return []
        for (name, ref), (_, modified) in zip(old_defaults, new_defaults):
            if isinstance(ref, ast.Constant) and isinstance(modified, ast.Constant):
                if type(ref.value) is type(modified.value) and type(ref.value) in SCALARS and ref.value != modified.value:
                    changes.append((a.lineno, a.name, name.arg))
                    modified.value = ref.value
    return changes if ast.dump(old) == ast.dump(new) else []


class ObservationIndex:
    def __init__(self, impact, filename):
        self.impact = impact
        self.details = impact.details
        self.base = impact.root
        data = json.loads(zlib.decompress(filename.read_bytes()))
        self.defaults = {key: int(bits, 16) for key, bits in data["defaults"].items()}
        self.values = data["values"]

    @lru_cache(maxsize=96)
    def original(self, path):
        text = (self.base / path).read_text()
        return text, ast.parse(text, path)

    @lru_cache(maxsize=1024)
    def observed_values(self, site):
        return [(None if values is None else tuple(v for _, v in values), int(bits, 16))
                for values, bits in self.values.get(site, ())]

    def edit(self, text):
        try:
            edits = split_diff(text)
            if len(edits) != 1:
                return None
            path, hunks = next(iter(edits.items()))
            if not path.startswith("sqlglot/") or not path.endswith(".py") or path.endswith("/__init__.py"):
                return None
            if path not in self.details.lines:
                return None
            source, old = self.original(path)
            modified = patched(source, hunks)
            after = ast.parse(modified, path)
            external = (self.impact.files.get(path, {}).get(0, 0) |
                        self.details.flow.opaque | self.details.positions)
            scalar = replacements(old, after)
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
                compile(after, path, "exec", dont_inherit=True)
                return "scalar", affected

            declarations = default_changes(old, after)
            if declarations:
                affected = external
                for line, function, arg in declarations:
                    bits = self.defaults.get(f"{path}:{line}:{function}:{arg}")
                    if bits is None:
                        return None
                    affected |= bits
                # No new binding, definition execution or registry mutation.
                # Each changed value is an existing scalar literal.
                return "default", affected
        except (UnknownChange, OSError, ValueError, SyntaxError, RecursionError):
            return None
        return None
