"""Check upstream TPC-DS inputs and run queries with changed SQL against both engines."""
import hashlib
import json
import pickle
import sys
import zlib
from pathlib import Path

from checkpoint import digest

STORE = Path(__file__).resolve().parent / "tpcds_sql.json"
PARSED = STORE.with_name("tpcds_parsed.z")


def fingerprint(text):
    return hashlib.sha256(text.encode()).hexdigest()


def inputs():
    from tests.helpers import load_sql_fixture_pairs
    return list(load_sql_fixture_pairs("optimizer/tpc-ds/tpc-ds.sql"))


def sql_boundary(sql):
    from sqlglot import parse_one
    tree = pickle.loads(pickle.dumps(parse_one(sql)))
    return tree, fingerprint(tree.sql()), fingerprint(tree.sql(dialect="duckdb"))


def optimized_sql(tree):
    from sqlglot.optimizer import optimize
    from tests.helpers import TPCDS_SCHEMA
    result = optimize(tree, schema=TPCDS_SCHEMA, leave_tables_isolated=True)
    return fingerprint(result.sql())


def build():
    from sqlglot import __file__ as package
    package_root = str(Path(package).resolve().parent.parent) + "/"
    monitor = sys.monitoring
    tid = 3
    observed = set()

    def record(code, *event):
        if code.co_filename.startswith(package_root + "sqlglot/"):
            observed.add(code.co_filename[len(package_root):])

    monitor.use_tool_id(tid, "TPC-DS parse and serialization coverage")
    monitor.register_callback(tid, monitor.events.PY_START, record)
    rows, snapshots = [], {}
    try:
        for i, (meta, sql, _) in enumerate(inputs()):
            monitor.set_events(tid, monitor.events.PY_START)
            tree, parsed, duck = sql_boundary(sql)
            monitor.set_events(tid, 0)
            execute = bool(meta.get("execute"))
            key = digest(tree) if execute else None
            if execute:
                snapshots[i] = pickle.dumps(tree)
            rows.append([fingerprint(sql), execute, parsed, duck,
                         optimized_sql(tree) if execute else None, key])
    finally:
        monitor.set_events(tid, 0)
        monitor.free_tool_id(tid)
    if len(snapshots) != 57:
        raise ValueError("incomplete TPC-DS fixture")
    STORE.write_text(json.dumps({"rows": rows, "boundary_files": sorted(observed)}))
    PARSED.write_bytes(zlib.compress(pickle.dumps(snapshots)))


def check(first=None, reuse_file=None):
    from contextlib import ExitStack
    from tests.helpers import TPCDS_SCHEMA

    pairs = inputs()
    model = json.loads(STORE.read_bytes())
    expected = model["rows"]
    snapshots = None
    if reuse_file and reuse_file not in model["boundary_files"]:
        snapshots = pickle.loads(zlib.decompress(PARSED.read_bytes()))
    case = None
    with ExitStack() as cleanup:
        rows = list(enumerate(zip(pairs, expected, strict=True)))
        if first is not None:
            rows.sort(key=lambda row: row[1][1][-1] != first)
        for i, ((meta, sql, _), (source, execute_query, parsed, duck, optimized, _)) in rows:
            if fingerprint(sql) != source or bool(meta.get("execute")) != execute_query:
                return False
            if snapshots is None:
                tree, p, d = sql_boundary(sql)
            elif execute_query:
                tree, p, d = pickle.loads(snapshots[i]), parsed, duck
            else:
                continue
            if not execute_query or (optimized_sql(tree) == optimized and d == duck and p == parsed):
                continue
            if case is None:
                from sqlglot import find_tables
                from sqlglot.executor import execute
                from tests.test_executor import TestExecutor

                case = TestExecutor("test_execute_tpcds")
                case.setUpClass()
                cleanup.callback(case.tearDownClass)
            subset = {t.name: case.tpcds_tables[t.name] for t in find_tables(tree)}
            table = execute(tree, schema=TPCDS_SCHEMA, tables=subset)
            case.subtestHelper(i, table, tpch=False)
    return True


if __name__ == "__main__":
    build()
