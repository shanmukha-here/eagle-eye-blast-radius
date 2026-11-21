"""Replay the DuckDB reference side when the unchanged test only reads a setting there."""

import hashlib
import json
import os
import pickle
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

from dependencies import scalar_declarations
from impact import patched, split_diff, UnknownChange
from refine import changed_fields

DIRECTORY = Path(__file__).resolve().parent
STORE = DIRECTORY / "reference_results.json"
TARGETS = {"test_execute_tpcds": "tpcds", "test_optimized_tpch": "tpch"}


def hash_bytes(data):
    return hashlib.sha256(data).hexdigest()


def queries(suite):
    from tests.helpers import load_sql_fixture_pairs

    pairs = load_sql_fixture_pairs(
        "optimizer/tpc-ds/tpc-ds.sql" if suite == "tpcds" else "optimizer/tpc-h/tpc-h.sql"
    )
    if suite == "tpcds":
        return [sql for meta, sql, _ in pairs if meta.get("execute")]
    return [text for _, orig, optimized in pairs for text in (orig, optimized)]


def connection(suite, materialize=False):
    import duckdb
    from tests.test_executor import DIR_TPCDS, DIR_TPCH, TPCDS_SCHEMA, TPCH_SCHEMA

    data = DIRECTORY / f"reference-{suite}.duckdb"
    conn = duckdb.connect(str(data) if materialize else ":memory:")
    directory, schema = (DIR_TPCDS, TPCDS_SCHEMA) if suite == "tpcds" else (DIR_TPCH, TPCH_SCHEMA)
    try:
        for name, types in schema.items():
            path = directory + name + ".csv.gz"
            conn.execute(
                f"CREATE {'TABLE' if materialize else 'VIEW'} {name} AS "
                f"SELECT * FROM READ_CSV('{path}', delim='|', header=True, columns={types})"
            )
    except BaseException:
        conn.close()
        raise
    return conn


def result_digest(conn, sql):
    return hash_bytes(pickle.dumps(conn.execute(sql).fetchdf(), protocol=5))


def native_digest(conn, sql, null_order):
    """Compare parsed statements with the pinned connection's binding defaults."""
    if null_order not in ("NULLS_LAST", "NULLS_FIRST"):
        return None
    ast = json.loads(conn.execute("SELECT json_serialize_sql(?)", [sql]).fetchone()[0])
    if ast.get("error") or len(ast.get("statements", ())) != 1:
        return None
    default = null_order.replace("_", " ")
    pending = [ast]
    while pending:
        node = pending.pop()
        if isinstance(node, dict):
            if node.get("function_name") in (
                "current_query", "query", "query_table", "duckdb_prepared_statements"
            ):
                return None
            node.pop("query_location", None)
            node.pop("query_location_length", None)
            # ORDER_DEFAULT binds to the same value for both directions under
            # the fixed null-order settings admitted here.
            if {"type", "null_order", "expression"} <= node.keys() and node["null_order"] == "ORDER_DEFAULT":
                node["null_order"] = default
            pending.extend(node.values())
        elif isinstance(node, list):
            pending.extend(node)
    return hash_bytes(json.dumps(ast, sort_keys=True, separators=(",", ":")).encode())


def default_order(conn):
    return conn.execute("SELECT current_setting('default_null_order')").fetchone()[0]


def build():
    from sqlglot import transpile

    references = {}
    for suite, expected in (("tpcds", 57), ("tpch", 44)):
        source = queries(suite)
        if len(source) != expected:
            raise ValueError("fixture count differs from the completed base test")
        data = DIRECTORY / f"reference-{suite}.duckdb"
        if data.exists():
            data.unlink()
        with connection(suite) as conn, connection(suite, materialize=True) as cached:
            order = default_order(conn)
            sql = [transpile(text, write="duckdb")[0] for text in source]
            rows = []
            for raw, rendered in zip(source, sql):
                digest = result_digest(conn, rendered)
                rows.append([hash_bytes(raw.encode()), hash_bytes(rendered.encode()),
                             native_digest(conn, rendered, order), digest,
                             result_digest(cached, rendered) == digest])
            references[suite] = rows
    STORE.write_text(json.dumps(references, separators=(",", ":")))


def verify(root, path, suite):
    import importlib

    from import_check import PatchedModule

    module = path[:-3].replace("/", ".")
    loader = PatchedModule(module, str(root / path), sys.stdin.buffer.read())
    sys.meta_path.insert(0, loader)
    sys.path.insert(0, str(root))
    importlib.import_module(module)
    if sys.modules[module].__loader__ is not loader:
        return False

    from sqlglot import transpile

    old = json.loads(STORE.read_bytes())[suite]
    changed = []
    for raw, (source, rendered, normalized, rows, cached) in zip(queries(suite), old, strict=True):
        if hash_bytes(raw.encode()) != source:
            return False
        output = transpile(raw, write="duckdb")[0]
        if hash_bytes(output.encode()) != rendered:
            changed.append((output, normalized, rows, cached))
    if changed:
        import duckdb

        with duckdb.connect(str(DIRECTORY / f"reference-{suite}.duckdb"), read_only=True) as conn:
            order = default_order(conn)
            return all((normalized is not None and native_digest(conn, output, order) == normalized)
                       or cached and result_digest(conn, output) == rows
                       for output, normalized, rows, cached in changed)
    return True


class ReferenceReplay:
    def __init__(self, base, directory, scheduler, tests, count):
        self.base = base.resolve()
        self.roles = json.loads((directory / "reference_reads.json").read_bytes())
        self.scheduler = scheduler
        self.tests = {r["number"]: TARGETS[r["id"].rsplit("::", 1)[-1]] for r in tests
                      if r["id"].rsplit("::", 1)[-1] in TARGETS}
        self.budget = min(24, 0.4 * count)

    @lru_cache(maxsize=128)
    def original(self, path):
        return (self.base / path).read_text()

    def apply(self, patch, plan):
        if self.budget < 0.4:
            return plan
        selected = {number for group in plan for number in group}
        long = selected & self.tests.keys()
        if not long:
            return plan
        try:
            edits = split_diff(patch)
            if len(edits) != 1:
                return plan
            path, hunks = next(iter(edits.items()))
            if not path.startswith("sqlglot/dialects/") or not path.endswith(".py"):
                return plan
            original = self.original(path)
            modified = patched(original, hunks)
            fields = changed_fields(original, modified)
            definitions = scalar_declarations(original)
            if not fields or not all(pair in definitions for pair in fields):
                return plan
            keys = {f"field:{path}:{owner}:{field}" for owner, field in fields}
            for number in sorted(long, key=self.scheduler.durations.get, reverse=True):
                suite = self.tests[number]
                if not keys <= set(self.roles[suite]):
                    continue
                reduced = self.scheduler.repack(selected - {number}, plan)
                gain = (self.scheduler.cost(plan) - self.scheduler.cost(reduced)) / 1000
                cap = min(self.budget, 3.0, gain * 0.65)
                if cap < 0.45:
                    continue
                self.budget -= cap
                result = subprocess.run(
                    [sys.executable, "-B", __file__, "verify", str(self.base), path, suite],
                    input=modified.encode(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    timeout=cap, env=dict(os.environ, PYTHONHASHSEED="0"), check=False,
                )
                if result.returncode == 0:
                    selected.remove(number)
                    plan = reduced
        except (UnknownChange, OSError, SyntaxError, ValueError, RecursionError, subprocess.TimeoutExpired):
            pass
        return plan


if __name__ == "__main__":
    if sys.argv[1] == "build":
        build()
    else:
        root, path, suite = sys.argv[2:]
        raise SystemExit(0 if verify(Path(root), path, suite) else 1)
