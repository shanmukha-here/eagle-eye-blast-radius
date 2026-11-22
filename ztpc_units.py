"""One TPC query as the executor tests run it, reduced to a digest of what sqlglot returns.

`_mp_execute` parses the query and pickles it to a pool worker, which executes it and
pickles the table back. `subtestHelper` compares that table with DuckDB running sqlglot's
DuckDB transpilation of the same text. The digest covers the pickled expression, the pickled
table and the transpiled text, so equal digests give equal comparisons.
"""

import hashlib
import pickle


def run_query(klass, name, i):
    import tests.test_executor as te
    from sqlglot import parse_one, transpile

    if name == "tpcds":
        sqls, schema, tables = klass.tpcds_sqls, te.TPCDS_SCHEMA, klass.tpcds_tables
    else:
        sqls, schema, tables = klass.tpch_sqls, te.TPCH_SCHEMA, klass.tpch_tables
    meta, sql, _ = sqls[i]
    digest = hashlib.sha256()
    sent = pickle.dumps(parse_one(sql))
    digest.update(sent)
    if meta.get("execute"):
        te.initializer(schema, tables)
        table = te.mp_execute(pickle.loads(sent), meta)
        digest.update(pickle.dumps(table))
        digest.update(transpile(sql, write="duckdb")[0].encode())
    return digest.hexdigest()


class Tables:
    """The executor tests' TPC fixtures, loaded the way their setUpClass loads them,
    without the DuckDB views that only the DuckDB side of the comparison uses."""

    def __init__(self):
        import ast
        import csv
        import tests.test_executor as te
        from sqlglot.executor.table import Table
        from tests.helpers import load_sql_fixture_pairs

        def setup(directory, table, columns, tables):
            file_name = f"{directory}{table}.csv.gz"
            reader = csv.reader(te.open_file(file_name), delimiter="|")
            rows = []
            ctypes = []
            next(reader)
            for row in reader:
                if not ctypes:
                    for v in row:
                        try:
                            ctypes.append(type(ast.literal_eval(v)))
                        except (ValueError, SyntaxError):
                            ctypes.append(str)
                rows.append(
                    tuple(None if (t is not str and v == "") else t(v) for t, v in zip(ctypes, row))
                )
            tables[table] = Table(columns=columns, rows=rows)

        self.tpch_tables = {}
        self.tpcds_tables = {}
        for table, columns in te.TPCH_SCHEMA.items():
            setup(te.DIR_TPCH, table, columns, self.tpch_tables)
        for table, columns in te.TPCDS_SCHEMA.items():
            setup(te.DIR_TPCDS, table, columns, self.tpcds_tables)
        self.tpch_sqls = list(load_sql_fixture_pairs("optimizer/tpc-h/tpc-h.sql"))
        self.tpcds_sqls = list(load_sql_fixture_pairs("optimizer/tpc-ds/tpc-ds.sql"))
