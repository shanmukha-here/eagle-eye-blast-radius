"""Validate name-cache bypass using all hits in the clean TPC-DS replay."""
import ast
import pickle
import zlib
from pathlib import Path
from types import SimpleNamespace


SNAPSHOTS = Path(__file__).with_name("tpcds_cache_inputs.z")


def record_hits():
    from sqlglot import exp
    from sqlglot.schema import MappingSchema

    original = MappingSchema._normalize_name
    records = set()
    supported = True
    calls = 0

    def observe(self, name, dialect=None, is_table=False, normalize=None):
        nonlocal supported, calls
        calls += 1
        resolved = dialect or self.dialect
        setting = self.normalize if normalize is None else normalize
        key = (name if isinstance(name, str) else name.name, resolved, is_table, setting)
        value = self._normalized_name_cache.get(key)
        if value:
            # The base Dialect's normalizer only mutates its copied identifier.
            if (type(self) is not MappingSchema or type(name) not in (str, exp.Identifier)
                    or resolved not in (None, "") or type(value) is not str
                    or type(setting) is not bool or type(is_table) is not bool):
                supported = False
            else:
                records.add(pickle.dumps((name, resolved, is_table, setting, value)))
        return original(self, name, dialect=dialect, is_table=is_table, normalize=normalize)

    def finish():
        MappingSchema._normalize_name = original
        SNAPSHOTS.write_bytes(zlib.compress(pickle.dumps(sorted(records) if supported and calls else None)))

    MappingSchema._normalize_name = observe
    return finish


def bypass_only(before, after):
    """Require removal of precisely the known memoized early return."""
    if not isinstance(before, ast.Module) or not isinstance(after, ast.Module):
        return False
    if len(before.body) != len(after.body):
        return False
    changed = [(a, b) for a, b in zip(before.body, after.body) if ast.dump(a) != ast.dump(b)]
    if len(changed) != 1:
        return False
    a, b = changed[0]
    return (
        isinstance(a, ast.If) and isinstance(b, ast.If)
        and ast.unparse(a.test) == "(cached := self._normalized_name_cache.get(cache_key))"
        and len(a.body) == 1 and ast.unparse(a.body[0]) == "return cached"
        and not a.orelse and not b.orelse
        and isinstance(b.test, ast.Constant) and b.test.value is False
        and len(b.body) == 1 and ast.unparse(b.body[0]) == "return cached"
    )


def same_cache_results():
    from sqlglot.schema import MappingSchema

    records = pickle.loads(zlib.decompress(SNAPSHOTS.read_bytes()))
    if records is None:
        return False
    for raw in records:
        name, dialect, is_table, normalize, expected = pickle.loads(raw)
        key = (name if isinstance(name, str) else name.name, dialect, is_table, normalize)
        cache = {key: expected}
        obj = SimpleNamespace(normalize=normalize, dialect=dialect, _normalized_name_cache=cache)
        result = MappingSchema._normalize_name(obj, name, dialect, is_table, normalize)
        if type(result) is not str or result != expected or cache != {key: expected}:
            return False
    return True
