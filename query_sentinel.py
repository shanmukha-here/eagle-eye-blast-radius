"""Check upstream TPC-DS inputs and run queries with changed SQL against both engines."""
import hashlib
import json
import pickle
from pathlib import Path

from checkpoint import digest

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
        execute = bool(meta.get("execute"))
        key = digest(tree) if execute else None
        rows.append([fingerprint(sql), execute, parsed, duck,
                     optimized_sql(tree) if execute else None, key])
    if sum(r[1] for r in rows) != 57:
        raise ValueError("incomplete TPC-DS fixture")
    STORE.write_text(json.dumps(rows))


def check(first=None):
    from contextlib import ExitStack
    from tests.helpers import TPCDS_SCHEMA

    pairs = inputs()
    expected = json.loads(STORE.read_bytes())
    case = None
    with ExitStack() as cleanup:
        rows = list(enumerate(zip(pairs, expected, strict=True)))
        if first is not None:
            rows.sort(key=lambda row: row[1][1][-1] != first)
        for i, ((meta, sql, _), (source, execute_query, parsed, duck, optimized, _)) in rows:
            if fingerprint(sql) != source or bool(meta.get("execute")) != execute_query:
                return False
            tree, p, d = sql_boundary(sql)
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
