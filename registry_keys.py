"""Identify dispatch tables and edits isolated to one stable handler slot."""
import ast
from functools import lru_cache

from registry_guard import DISPATCH


def dispatch_name(name):
    return name in {"_classes", "classes", "NUMERIC_ESCAPES"} or bool(DISPATCH.fullmatch(name))


def assignment(node):
    if isinstance(node, ast.Assign) and len(node.targets) == 1:
        target = node.targets[0]
    elif isinstance(node, ast.AnnAssign):
        target = node.target
    else:
        return None
    return target.id if isinstance(target, ast.Name) and dispatch_name(target.id) else None


def tables(body, scope=""):
    for stmt in body:
        if isinstance(stmt, ast.ClassDef):
            yield from tables(stmt.body, scope + stmt.name + ".")
        elif (name := assignment(stmt)) is not None:
            yield scope + name, stmt


def normalize_args(call):
    def literal(expr):
        if isinstance(expr, ast.Constant) and type(expr.value) in (bool, int, str, float, type(None)):
            return ast.Name(id="_literal_" + type(expr.value).__name__, ctx=ast.Load())
        return None
    if not isinstance(call.func, ast.Name) or any(k.arg is None for k in call.keywords):
        return None
    args = [literal(a) for a in call.args]
    keys = [(k.arg, literal(k.value)) for k in call.keywords]
    if any(a is None for a in args) or any(a is None for _, a in keys):
        return None
    return ast.dump(ast.Call(func=call.func, args=args, keywords=[ast.keyword(arg=k, value=v) for k, v in keys]))


@lru_cache(maxsize=128)
def factory_defs(root, path, source, name):
    original = ast.parse(source)
    for node in original.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return (node,)
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module and node.module.startswith("sqlglot."):
            for alias in node.names:
                if (alias.asname or alias.name) != name:
                    continue
                filename = node.module.replace(".", "/") + ".py"
                imported = (root / filename).read_text()
                return tuple(n for n in ast.parse(imported).body
                             if isinstance(n, ast.FunctionDef) and n.name == alias.name)
    return ()


def pure_factory(root, path, source, name):
    for fn in factory_defs(root, path, source, name):
        if fn.decorator_list or len(fn.body) != 2:
            continue
        child, result = fn.body
        if (isinstance(child, ast.FunctionDef) and not child.decorator_list
                and not child.args.defaults and not any(child.args.kw_defaults)
                and isinstance(result, ast.Return) and isinstance(result.value, ast.Name)
                and result.value.id == child.name):
            return True
    return False


def one_entry(root, path, source, after):
    old = ast.parse(source)
    current = ast.parse(after)
    revisited = dict(tables(current.body))
    outer = ast.dump(old)
    for name, original in tables(old.body):
        other = revisited.get(name)
        if (other is None or not isinstance(original.value, ast.Dict)
                or not isinstance(other.value, ast.Dict) or len(original.value.keys) != len(other.value.keys)):
            continue
        left, right = original.value, other.value
        if [ast.dump(k) if k is not None else None for k in left.keys] != [
            ast.dump(k) if k is not None else None for k in right.keys
        ]:
            continue
        modified = [i for i, (a, b) in enumerate(zip(left.values, right.values)) if ast.dump(a) != ast.dump(b)]
        if len(modified) != 1:
            continue
        i, = modified
        key = left.keys[i]
        if not isinstance(key, ast.Constant) or not isinstance(key.value, str):
            continue
        a, b = left.values[i], right.values[i]
        if isinstance(a, ast.Lambda) and isinstance(b, ast.Lambda):
            admitted = ast.dump(a.args) == ast.dump(b.args)
        elif isinstance(a, ast.Call) and isinstance(b, ast.Call):
            normal = normalize_args(a)
            admitted = normal is not None and normal == normalize_args(b) and pure_factory(root, path, source, a.func.id)
        elif name.rsplit(".", 1)[-1] == "NUMERIC_ESCAPES":
            # Settings look up a known escape prefix. Keep the table shape
            # and tuple layout fixed so construction remains equivalent.
            admitted = all(isinstance(node, ast.Tuple) and len(node.elts) == 4
                           and all(isinstance(value, ast.Constant) and type(value.value) is int
                                   for value in node.elts)
                           for node in (a, b))
        else:
            admitted = False
        if not admitted:
            continue
        right.values[i] = a
        if ast.dump(current) == outer:
            return name, key.value
        right.values[i] = b
    return None


def literal_table(before, after):
    """Identify one class dispatch table made entirely of literals."""
    original = ast.dump(before)
    revisited = dict(tables(after.body))
    for owner, left in tables(before.body):
        right = revisited.get(owner)
        if "." not in owner or right is None or ast.dump(left) == ast.dump(right):
            continue
        if not all(isinstance(node.value, ast.Dict) for node in (left, right)):
            continue
        if any(key is None or not isinstance(key, ast.Constant) or not isinstance(key.value, str)
               for node in (left, right) for key in node.value.keys):
            continue
        revised = right.value
        try:
            for node in (left.value, revised):
                ast.literal_eval(node)
            right.value = left.value
            if ast.dump(after) == original:
                return owner
        except (ValueError, TypeError):
            continue
        finally:
            right.value = revised
    return None
