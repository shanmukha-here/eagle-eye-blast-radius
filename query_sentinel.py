"""Check upstream TPC-DS inputs and run queries with changed SQL against both engines."""
import hashlib
import json
import pickle
from pathlib import Path

STORE = Path(__file__).resolve().parent / "tpcds_sql.json"


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
    rows = []
    for meta, sql, _ in inputs():
        tree, parsed, duck = sql_boundary(sql)
        rows.append([fingerprint(sql), bool(meta.get("execute")), parsed, duck,
                     optimized_sql(tree) if meta.get("execute") else None])
    if sum(r[1] for r in rows) != 57:
        raise ValueError("incomplete TPC-DS fixture")
    STORE.write_text(json.dumps(rows))


def check(optimizer_changed):
    from tests.helpers import TPCDS_SCHEMA
    pairs = inputs()
    expected = json.loads(STORE.read_bytes())
    changed = []
    for i, ((meta, sql, _), (source, execute, parsed, duck, optimized)) in enumerate(
            zip(pairs, expected, strict=True)):
        if fingerprint(sql) != source or bool(meta.get("execute")) != execute:
            return False
        tree, p, d = sql_boundary(sql)
        if execute and (p != parsed or d != duck or optimizer_changed):
            if d != duck or optimized_sql(tree) != optimized or p != parsed:
                changed.append((i, tree))
    if not changed:
        return True
    from sqlglot import find_tables
    from sqlglot.executor import execute
    from tests.test_executor import TestExecutor
    # The upstream subTest assertions raise outside unittest's run context.
    case = TestExecutor("test_execute_tpcds")
    case.setUpClass()
    try:
        for i, tree in changed:
            subset = {t.name:case.tpcds_tables[t.name] for t in find_tables(tree)}
            table = execute(tree, schema=TPCDS_SCHEMA, tables=subset)
            case.subtestHelper(i, table, tpch=False)
    finally:
        case.tearDownClass()
    return True


if __name__ == "__main__":
    build()
