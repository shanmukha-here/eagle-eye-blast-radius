"""Bound impact of isolated collection failures, scalar fields and added source files."""

import ast
import hashlib
import re
import sys
from functools import lru_cache
from pathlib import PurePosixPath


def name_bits(name, size):
    digest = hashlib.sha256(name).digest()
    return [
        int.from_bytes(digest[i : i + 4], "big") % (size * 8) for i in (0, 4, 8, 12)
    ]


class Dependencies:
    def __init__(self, data):
        self.seen = {key: int(bits, 16) for key, bits in data["events"].items()}
        self.names = bytes.fromhex(data["identifiers"])

    def scalar_users(self, path, owner, field, source):
        if (owner, field) not in scalar_declarations(source):
            return None
        return self.seen.get(f"field:{path}:{owner}:{field}")

    def addition(self, root, path, source, full):
        f = PurePosixPath(path)
        if f.parent.as_posix() != "sqlglot" or f.suffix != ".py":
            return full
        name = f.stem
        known = all(
            self.names[i // 8] & (1 << (i % 8))
            for i in name_bits(name.encode(), len(self.names))
        )
        if (
            not re.fullmatch(r"[a-z][a-z0-9_]*", name)
            or name.startswith("test")
            or name.endswith("_test")
            or known
            or name in sys.stdlib_module_names
            or (root / f).exists()
            or (root / f.with_suffix("")).exists()
        ):
            return full
        tree = ast.parse(source)
        for node in tree.body:
            if (
                isinstance(node, ast.Expr)
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)
            ):
                continue
            if (
                not isinstance(node, ast.Assign)
                or len(node.targets) != 1
                or not isinstance(node.targets[0], ast.Name)
                or node.targets[0].id.startswith("__")
                or not isinstance(node.value, ast.Constant)
            ):
                return full
        compile(tree, path, "exec", dont_inherit=True)
        keys = ("dir:?", "dir:sqlglot", "module:sqlglot." + name, "path:" + f.name)
        selected = 0
        for key in keys:
            selected |= self.seen.get(key, 0)
        return selected


@lru_cache(maxsize=96)
def scalar_declarations(source):
    fields = set()
    for cls in ast.parse(source).body:
        if not isinstance(cls, ast.ClassDef):
            continue
        loads = {
            n.id
            for n in ast.walk(cls)
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
        }
        for stmt in cls.body:
            if (
                not isinstance(stmt, ast.Assign)
                or len(stmt.targets) != 1
                or not isinstance(stmt.targets[0], ast.Name)
            ):
                continue
            name = stmt.targets[0].id
            if name in loads or not name.isupper():
                continue
            try:
                value = ast.literal_eval(stmt.value)
            except (ValueError, TypeError):
                continue
            if type(value) in (bool, int, float, str):
                fields.add((cls.name, name))
    return fields


def appended_failure(source, modified):
    # Earlier module statements still execute with unchanged definitions.
    if not modified.startswith(source.rstrip("\n") + "\n"):
        return False
    tail = modified[len(source.rstrip("\n") + "\n") :]
    body = ast.parse(tail).body
    if len(body) != 1:
        return False
    stmt = body[0]
    if isinstance(stmt, ast.Raise) and stmt.cause is None:
        exc = stmt.exc
        if isinstance(exc, ast.Call):
            if exc.keywords or any(not isinstance(a, ast.Constant) for a in exc.args):
                return False
            exc = exc.func
        if not isinstance(exc, ast.Name) or exc.id not in (
            "AssertionError",
            "ImportError",
            "RuntimeError",
            "ValueError",
        ):
            return False
        # A local alias for the exception constructor can execute arbitrary code.
        return not any(
            isinstance(n, ast.Name)
            and n.id == exc.id
            and isinstance(n.ctx, ast.Store)
            or isinstance(n, (ast.FunctionDef, ast.ClassDef))
            and n.name == exc.id
            or isinstance(n, ast.alias)
            and (n.asname or n.name) in ("*", exc.id)
            for n in ast.walk(ast.parse(source))
        )
    if (
        isinstance(stmt, ast.ImportFrom)
        and stmt.level == 0
        and stmt.module in ("sys", "builtins")
    ):
        module = sys.modules[stmt.module]
        return any(n.name != "*" and not hasattr(module, n.name) for n in stmt.names)
    return False


@lru_cache(maxsize=96)
def simple_collection(source):
    # Skip files with arbitrary import-time setup, registered hooks or dynamic plugins.
    for node in ast.parse(source).body:
        if isinstance(node, (ast.ImportFrom, ast.Import)):
            continue
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            continue
        if isinstance(node, ast.ClassDef):
            if (
                node.decorator_list
                or node.keywords
                or not all(isinstance(b, (ast.Name, ast.Attribute)) for b in node.bases)
            ):
                return False
            for stmt in node.body:
                if (
                    isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and not stmt.decorator_list
                ):
                    evaluated = [
                        *stmt.args.defaults,
                        *stmt.args.kw_defaults,
                        stmt.returns,
                    ]
                    evaluated.extend(
                        a.annotation
                        for a in (
                            *stmt.args.posonlyargs,
                            *stmt.args.args,
                            *stmt.args.kwonlyargs,
                        )
                    )
                    if stmt.args.vararg:
                        evaluated.append(stmt.args.vararg.annotation)
                    if stmt.args.kwarg:
                        evaluated.append(stmt.args.kwarg.annotation)
                    if any(
                        isinstance(n, (ast.Call, ast.NamedExpr))
                        for expr in evaluated
                        if expr
                        for n in ast.walk(expr)
                    ):
                        return False
                    continue
                if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant):
                    continue
                if (
                    isinstance(stmt, ast.Assign)
                    and all(isinstance(t, ast.Name) for t in stmt.targets)
                    and isinstance(stmt.value, ast.Constant)
                ):
                    continue
                return False
            continue
        return False
    return True
