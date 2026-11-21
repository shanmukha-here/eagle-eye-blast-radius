"""Reconstruct verified replay inputs at build time from the pinned public fixtures."""

import ast
import csv
import gzip
import json
import pickle
import sys
import zlib
from pathlib import Path

from checkpoint import digest, table_digest
from sqlglot import find_tables, parse_one
from sqlglot.executor import execute
from sqlglot.executor.table import Table
from sqlglot.optimizer import optimize
from sqlglot.schema import ensure_schema
from tests.helpers import FIXTURES_DIR, TPCDS_SCHEMA, load_sql_fixture_pairs


def encode(obj):
    return zlib.compress(pickle.dumps(obj, protocol=5), 6)


def fixture_table(name):
    # This is the typed CSV conversion used by the pinned TestExecutor.setUpClass.
    filename = Path(FIXTURES_DIR) / "optimizer" / "tpc-ds" / (name + ".csv.gz")
    with gzip.open(filename, "rt", newline="") as f:
        reader = csv.reader(f, delimiter="|")
        next(reader)
        types, rows = [], []
        for row in reader:
            if not types:
                for v in row:
                    try:
                        types.append(type(ast.literal_eval(v)))
                    except (ValueError, SyntaxError):
                        types.append(str)
            rows.append(tuple(None if t is not str and v == "" else t(v) for t, v in zip(types, row)))
    return Table(columns=TPCDS_SCHEMA[name], rows=rows)


def main(index_file, output):
    expected = {key: (table, opt) for key, table, opt in json.loads(zlib.decompress(index_file.read_bytes()))["cases"]}
    output.mkdir(parents=True, exist_ok=True)
    tables, matched = {}, set()
    for meta, sql, _ in load_sql_fixture_pairs("optimizer/tpc-ds/tpc-ds.sql"):
        if not meta.get("execute"):
            continue
        tree = pickle.loads(pickle.dumps(parse_one(sql), protocol=5))
        key = digest(tree)
        if key not in expected or key in matched:
            continue
        names = sorted({t.name for t in find_tables(tree)})
        for name in names:
            if name not in tables:
                tables[name] = fixture_table(name)
                (output / ("table-" + name + ".z")).write_bytes(encode(tables[name]))
        args = (tree, ensure_schema(TPCDS_SCHEMA, dialect=None))
        kwargs = {"leave_tables_isolated": True, "dialect": None}
        raw = encode((args, kwargs, names))
        args, kwargs, _ = pickle.loads(zlib.decompress(raw))
        optimized = optimize(*args, **kwargs)
        opt_result = digest(args[0], optimized)
        args, _, _ = pickle.loads(zlib.decompress(raw))
        table = execute(args[0], schema=TPCDS_SCHEMA, tables={n: tables[n] for n in names})
        reference, opt_reference = expected[key]
        if table_digest(table) == reference and (not opt_reference or opt_reference == opt_result):
            (output / (key + ".z")).write_bytes(raw)
            matched.add(key)
    # Individual missing references disable the corresponding runtime probe.
    print(f"verified {len(matched)} of {len(expected)} upstream query postconditions")


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]))
