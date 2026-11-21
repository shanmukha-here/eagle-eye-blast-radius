"""Narrow two import effects with observations from the clean interpreter."""
import ast
import copy
import json
import re
import zlib
from pathlib import Path

from dependencies import name_bits


def cached_return(old, new):
    """The hook caches the same binding before returning a different literal."""
    if not isinstance(old, ast.Module) or not isinstance(new, ast.Module):
        return False
    left, right = copy.deepcopy(old), copy.deepcopy(new)
    changes = 0
    for node in ast.walk(left):
        body = getattr(node, "body", None)
        if not isinstance(body, list) or len(body) < 2:
            continue
        store, ret = body[-2:]
        if not (isinstance(ret, ast.Return) and isinstance(ret.value, ast.Name)
                and isinstance(store, ast.Assign) and len(store.targets) == 1
                and isinstance(store.value, ast.Name) and store.value.id == ret.value.id):
            continue
        target = store.targets[0]
        if not (isinstance(target, ast.Subscript) and isinstance(target.slice, ast.Name)
                and target.slice.id == "name" and isinstance(target.value, ast.Call)
                and isinstance(target.value.func, ast.Name) and target.value.func.id == "globals"
                and not target.value.args and not target.value.keywords):
            continue
        matches = [n for n in ast.walk(right) if isinstance(n, ast.Return)
                   and n.lineno == ret.lineno and isinstance(n.value, ast.Constant)]
        if len(matches) != 1:
            continue
        matches[0].value = ret.value
        changes += 1
    return changes == 1 and ast.dump(left) == ast.dump(right)


def derived_registry_fields(tree, old, new):
    """Account for every use of the registration enum in the pinned metaclass."""
    meta = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "_Dialect")
    make = next(n for n in meta.body if isinstance(n, ast.FunctionDef) and n.name == "__new__")
    fields = set()
    for statement in make.body:
        if not any(isinstance(n, ast.Name) and n.id == "enum" for n in ast.walk(statement)):
            continue
        form = ast.unparse(statement)
        if form in ("enum = Dialects.__members__.get(clsname.upper())",
                    "cls._classes[enum.value if enum is not None else clsname.lower()] = klass"):
            continue
        if not isinstance(statement, ast.If) or statement.orelse:
            return None
        cond = statement.test
        if not (isinstance(cond, ast.Compare) and isinstance(cond.left, ast.Name) and cond.left.id == "enum"
                and len(cond.ops) == 1 and isinstance(cond.ops[0], (ast.In, ast.NotIn))
                and isinstance(cond.comparators[0], ast.Tuple)
                and all(isinstance(n, ast.Constant) and type(n.value) is str for n in cond.comparators[0].elts)):
            return None
        values = {n.value for n in cond.comparators[0].elts}
        for assign in statement.body:
            if not (isinstance(assign, ast.Assign) and len(assign.targets) == 1
                    and isinstance(assign.value, ast.Constant) and isinstance(assign.targets[0], ast.Attribute)
                    and isinstance(assign.targets[0].value, ast.Name) and assign.targets[0].value.id == "klass"):
                return None
            if (old in values) != (new in values):
                fields.add(assign.targets[0].attr)
    return fields


def registry_edit(source, modified):
    left, right = ast.parse(source), ast.parse(modified)
    def values(tree):
        cls = next((n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Dialects"), None)
        if cls is None or [ast.unparse(b) for b in cls.bases] != ["str", "Enum"]:
            return {}
        return {n.targets[0].id: n.value for n in cls.body
                if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name)
                and isinstance(n.value, ast.Constant) and type(n.value.value) is str}
    a, b = values(left), values(right)
    if not a or a.keys() != b.keys() or len({n.value for n in b.values()}) != len(b):
        return None
    changed = [k for k in a if a[k].value != b[k].value]
    if len(changed) != 1:
        return None
    k = changed[0]
    before, after = a[k].value, b[k].value
    if not re.fullmatch(r"[a-z_][a-z0-9_]{0,60}", after):
        return None
    b[k].value = before
    if ast.dump(left) != ast.dump(right):
        return None
    fields = derived_registry_fields(left, before, after)
    return None if fields is None else (k, before, after, fields)


class Startup:
    def __init__(self, directory):
        rows = json.loads(zlib.decompress((Path(directory) / "startup.z").read_bytes()))
        self.bits = {key: int(value, 16) for key, value in rows.items()}

    def lazy(self, path, key, old, new):
        if key != ("__getattr__", 0) or not path.endswith("/__init__.py"):
            return None
        prefix = "lazy:" + path[:-12].replace("/", ".") + ":"
        if prefix + "defined" in self.bits and cached_return(old, new):
            return self.bits.get(prefix + "read", 0) | self.bits.get(prefix + "direct-hook", 0)
        return None

    def registry(self, path, source, modified, access):
        if path != "sqlglot/dialects/dialect.py" or "registry:verified" not in self.bits:
            return None
        edit = registry_edit(source, modified)
        if not edit:
            return None
        member, old, new, fields = edit
        known_names = access.names
        if all(known_names[i // 8] & (1 << (i % 8)) for i in name_bits(new.encode(), len(known_names))):
            return None
        readers = 0
        for field in fields:
            if f"field:{path}:Dialect:{field}" not in access.seen:
                return None
            for key, bits in access.seen.items():
                if key.startswith("field:") and key.endswith(":" + field):
                    readers |= bits
        mask = (readers | self.bits.get("registry:all", 0) | self.bits.get("registry:member:" + member, 0)
                | self.bits.get("registry:key:" + old, 0) | self.bits.get("registry:key:" + new, 0))
        return mask, member == "DIALECT" and old == ""
