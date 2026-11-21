"""Replay the affected clean optimizer calls before clearing an expensive test."""

import ast
import copy
import json
import os
import pickle
import subprocess
import sys
import zlib
from pathlib import Path

from checkpoint import digest, table_digest
from import_check import PatchedModule
from refine import regions


def local_edit(old, new):
    """Only allow value/control edits inside a single existing function body."""
    def inspect(tree):
        calls = set()
        stores = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Global, ast.Nonlocal, ast.Import, ast.ImportFrom)):
                return None
            if isinstance(node, ast.Call):
                name = ast.dump(node.func)
                if isinstance(node.func, ast.Name) and node.func.id in (
                    "eval", "exec", "open", "globals", "locals", "setattr", "delattr"
                ):
                    return None
                calls.add(name)
            if isinstance(node, (ast.Name, ast.Attribute, ast.Subscript)) and isinstance(node.ctx, (ast.Store, ast.Del)):
                stores.add(ast.dump(node))
        return calls, stores
    a, b = inspect(old), inspect(new)
    return a is not None and b is not None and b[0] <= a[0] and b[1] <= a[1]


def suppressed_join_bucket(old, new):
    """Removing a key insertion cannot affect a query with no candidate pairs."""
    if old.name != "hash_join":
        return None
    before, after = copy.deepcopy(old), copy.deepcopy(new)
    conditions = [n for n in ast.walk(before) if isinstance(n, ast.If)]
    replacements = [n for n in ast.walk(after) if isinstance(n, ast.If)]
    if len(conditions) != len(replacements):
        return None
    changed = 0
    for a, b in zip(conditions, replacements):
        if ast.dump(a.test) == ast.dump(b.test):
            continue
        if (ast.unparse(a.test) != "all((value is not None for value in key))"
                or not isinstance(b.test, ast.Constant) or b.test.value is not False
                or len(a.body) != 1 or ast.unparse(a.body[0]) not in
                ("results[key][0].append((index, reader.row))", "results[key][1].append((index, reader.row))")):
            return None
        b.test = a.test
        changed += 1
    if changed != 1 or ast.dump(before) != ast.dump(after):
        return None
    # Positive coverage of this assignment witnesses an actual Cartesian pair.
    rows = [n.lineno for n in ast.walk(before) if isinstance(n, ast.Assign)
            and ast.unparse(n) == "row = source_row + join_row"]
    return rows[0] if len(rows) == 1 else None


def cost(scheduler, mask):
    workers = json.loads(scheduler.schedule(mask))
    peak = max((sum(scheduler.durations[i] for i in group) for group in workers), default=0)
    return peak + 2000 * len(workers)


class Replay:
    def __init__(self, ids, root, directory):
        index = json.loads(zlib.decompress((directory / "replay_index.z").read_bytes()))
        self.test = 1 << ids.index(index["test"]) if index["test"] in ids else 0
        lines = json.loads(zlib.decompress((directory / "query_lines.z").read_bytes()))
        self.line_ids = lines["locations"]
        self.line_pool = lines["pool"]
        self.cases = index["cases"]
        self.uses = index["uses"]
        self.root = Path(root).resolve()
        self.directory = directory
        self.reserved_seconds = 0.0

    def select(self, path, source, key, old, new, mask, scheduler, same_bindings=False):
        if (not mask & self.test or scheduler is None
                or path.endswith("/__init__.py") or path == "sqlglot/serde.py" or key[-2].startswith("__")):
            return mask
        qualified = ".".join(str(x) for x in key[:-1])
        location = f"{path}:{old[0]}:{qualified}"
        cases = self.uses.get(location)
        if not local_edit(old[3], new[3]):
            return mask
        spans = regions(old[3], new[3])
        if same_bindings and len(spans) == 1 and spans[0][0] == spans[0][1]:
            index = self.line_ids.get(f"{location}@{spans[0][0]}")
            if index is not None:
                opt, whole = self.line_pool[index]
                if opt >= 0 and whole >= 0:
                    cases = [[i for i in range(len(self.cases)) if bits & (1 << i)]
                             for bits in (opt, whole)]
        if not cases:
            return mask
        reduced = mask & ~self.test
        benefit = (cost(scheduler, mask) - cost(scheduler, reduced)) / 1000
        if benefit < 5:
            return mask
        opt, whole = cases
        execution_only = (path == "sqlglot/executor/python.py" and key[0] == "PythonExecutor"
                          and not opt)
        if execution_only and qualified == "PythonExecutor.hash_join":
            pair_line = suppressed_join_bucket(old[3], new[3])
            witness = self.line_ids.get(f"{location}@{pair_line}")
            if witness is not None:
                outside, query = self.line_pool[witness]
                if outside == 0 and query > 0:
                    whole = [i for i in whole if query & (1 << i)]
        if not execution_only and (len(whole) > 32 or len(opt) > 32):
            return mask
        # Check cheap optimizer postconditions first. A changed result requires
        # executing the original query and comparing its complete table.
        queries = [[*self.cases[i], False] for i in opt] + [[*self.cases[i], True] for i in whole]
        payload = json.dumps([path, source, queries, execution_only]).encode()
        timeout = min(10 if execution_only else 5, benefit / 2)
        if self.reserved_seconds + timeout > 60:
            return mask
        self.reserved_seconds += timeout
        try:
            check = subprocess.run(
                [sys.executable, "-B", str(self.directory / "optimizer_probe.py"), str(self.root)],
                input=payload, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=timeout, check=False,
                env=dict(os.environ, PYTHONHASHSEED="0", PYTHONPATH=""),
            )
            if check.returncode == 0:
                return reduced
        except (OSError, subprocess.TimeoutExpired):
            pass
        return mask


def verify(root):
    path, modified, checks, execution_only = json.load(sys.stdin)
    module = path[:-3].replace("/", ".")
    sys.meta_path.insert(0, PatchedModule(module, str(root / path), modified))
    sys.path.insert(0, str(root))
    from sqlglot.optimizer import optimize

    store = Path(__file__).resolve().parent / "optimizer_inputs"
    if not checks:
        return False
    from sqlglot import find_tables
    from sqlglot.executor import execute
    from tests.helpers import TPCDS_SCHEMA

    tables = {}
    for key, reference, opt_reference, whole in checks:
        if execution_only:
            from sqlglot.executor.python import PythonExecutor
            from sqlglot.executor.table import ensure_tables

            snapshot = zlib.decompress((store / ("optimized-" + key + ".z")).read_bytes())
            plan, names = pickle.loads(snapshot)
            fresh = {}
            for name in names:
                if name not in tables:
                    tables[name] = zlib.decompress((store / ("table-" + name + ".z")).read_bytes())
                fresh[name] = pickle.loads(tables[name])
            result = PythonExecutor(tables=ensure_tables(fresh)).execute(plan)
            if table_digest(result) != reference:
                return False
            continue
        # Only a successful build-time comparison against the real test creates this file.
        raw = zlib.decompress((store / (key + ".z")).read_bytes())
        args, kwargs, names = pickle.loads(raw)
        if not whole and opt_reference:
            result = optimize(*args, **kwargs)
            if digest(args[0], result) == opt_reference:
                continue
            args, kwargs, names = pickle.loads(raw)
        observed_names = [t.name for t in find_tables(args[0])]
        if set(observed_names) != set(names):
            return False
        for n in observed_names:
            if n not in tables:
                tables[n] = pickle.loads(zlib.decompress((store / ("table-" + n + ".z")).read_bytes()))
        table = execute(args[0], schema=TPCDS_SCHEMA, tables={n: tables[n] for n in observed_names})
        if table_digest(table) != reference:
            return False
    return module in sys.modules


if __name__ == "__main__":
    raise SystemExit(0 if verify(Path(sys.argv[1])) else 1)
