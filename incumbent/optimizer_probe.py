"""Replay the affected clean optimizer calls before clearing an expensive test."""

import ast
import json
import os
import pickle
import subprocess
import sys
import time
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from checkpoint import digest, table_digest
from incumbent.import_check import PatchedModule


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


def cost(scheduler, mask):
    workers = json.loads(scheduler.schedule(mask))
    peak = max((sum(scheduler.durations[i] for i in group) for group in workers), default=0)
    return peak + 2000 * len(workers)


class Replay:
    def __init__(self, ids, root, directory):
        index = json.loads(zlib.decompress((directory / "replay_index.z").read_bytes()))
        self.test = 1 << ids.index(index["test"]) if index["test"] in ids else 0
        self.cases = index["cases"]
        self.uses = index["uses"]
        self.root = Path(root).resolve()
        self.directory = directory
        self.reserved_seconds = 0.0
        self.relevant = None
        self.last_seconds = 0.0
        self.last_mismatch = None

    def select(self, path, source, key, old, new, mask, scheduler):
        if (not mask & self.test or scheduler is None
                or path.endswith("/__init__.py") or path == "sqlglot/serde.py" or key[-2].startswith("__")):
            return mask
        qualified = ".".join(str(x) for x in key[:-1])
        cases = self.uses.get(f"{path}:{old[0]}:{qualified}")
        if not cases or not local_edit(old[3], new[3]):
            return mask
        reduced = mask & ~self.test
        benefit = (cost(scheduler, mask) - cost(scheduler, reduced)) / 1000
        if benefit < 5:
            return mask
        opt, whole = cases
        if len(whole) > 32:
            return mask
        # Check cheap optimizer postconditions first. A changed result requires
        # executing the original query and comparing its complete table.
        queries = [[*self.cases[i], False] for i in opt] + [[*self.cases[i], True] for i in whole]
        payload = json.dumps([path, source, queries]).encode()
        timeout = min(7, benefit / 2)
        if self.reserved_seconds + timeout > 60:
            return mask
        self.reserved_seconds += timeout
        if self.relevant is not None and not self.relevant(mask, reduced):
            return mask
        started = time.monotonic()
        try:
            check = subprocess.run(
                [sys.executable, "-B", str(self.directory / "optimizer_probe.py"), str(self.root)],
                input=payload, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                timeout=min(timeout, 4.0), check=False,
                env=dict(os.environ, PYTHONHASHSEED="0", PYTHONPATH=""),
            )
            if check.returncode == 0:
                return reduced
            witness = check.stdout.strip()
            if len(witness) == 64 and all(c in b"0123456789abcdef" for c in witness):
                self.last_mismatch = witness.decode("ascii")
        except (OSError, subprocess.TimeoutExpired):
            pass
        finally:
            self.last_seconds = time.monotonic() - started
        return mask


def verify(root):
    path, modified, checks = json.load(sys.stdin)
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
            print(key)
            return False
    return module in sys.modules


if __name__ == "__main__":
    raise SystemExit(0 if verify(Path(sys.argv[1])) else 1)
