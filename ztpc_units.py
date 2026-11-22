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

    def __init__(self, rows=True):
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
        self.tpch_sqls = list(load_sql_fixture_pairs("optimizer/tpc-h/tpc-h.sql"))
        self.tpcds_sqls = list(load_sql_fixture_pairs("optimizer/tpc-ds/tpc-ds.sql"))
        if not rows:
            return
        for table, columns in te.TPCH_SCHEMA.items():
            setup(te.DIR_TPCH, table, columns, self.tpch_tables)
        for table, columns in te.TPCDS_SCHEMA.items():
            setup(te.DIR_TPCDS, table, columns, self.tpcds_tables)


def _canon(value, sig):
    from sqlglot import exp
    from sqlglot.planner import Step

    if isinstance(value, Step):
        return ("S", sig(value))
    if isinstance(value, exp.Expr):
        return ("E", pickle.dumps(value))
    if isinstance(value, (set, frozenset)):
        return ("set", sorted((_canon(v, sig) for v in value), key=repr))
    if isinstance(value, dict):
        return ("dict", [(_canon(k, sig), _canon(v, sig)) for k, v in value.items()])
    if isinstance(value, (list, tuple)):
        return (type(value).__name__, [_canon(v, sig) for v in value])
    if isinstance(value, type):
        return ("T", value.__module__, value.__qualname__)
    if value is None or isinstance(value, (bool, int, float, str, bytes)):
        return value
    # Anything else may print an address, so the digest then differs from the pin's.
    return ("O", type(value).__qualname__, repr(value))


def plan_digest(plan):
    """A digest of every field of every plan step. Steps are named by a signature of their
    own fields and dependencies, so set and address order do not enter. `dependents` is the
    inverse of `dependencies` and is left out."""
    memo = {}

    def sig(step):
        key = id(step)
        if key not in memo:
            memo[key] = None  # a cycle shows as None
            fields = sorted((k, _canon(v, sig)) for k, v in vars(step).items()
                            if k not in ("dependencies", "dependents"))
            deps = sorted(sig(d) or b"" for d in step.dependencies)
            memo[key] = hashlib.sha256(repr((type(step).__qualname__, fields, deps)).encode()).digest()
        return memo[key]

    top = sorted((k, _canon(v, sig)) for k, v in vars(plan).items() if k != "_dag")
    return hashlib.sha256(repr((sig(plan.root), top)).encode()).digest()


def split_query(state, name, i, rows=True, phase=None):
    """The query as `sqlglot.executor.execute` runs it inside the test, in two phases.

    Returns (plan digest, full digest). The plan digest covers everything up to the row
    execution: the parse, the schema check, the optimized expression and its SQL text, the
    plan and the DuckDB transpilation. Row execution (`ensure_tables` and `PythonExecutor`)
    reads only the plan and the tables, so with unchanged code there it repeats the pin's
    result. ``phase(True|False)`` marks entering and leaving the row phase. With
    ``rows=False`` the row phase is skipped and the full digest is None.
    """
    import sqlglot.executor as ex
    import tests.test_executor as te
    from sqlglot import parse_one, transpile

    if name == "tpcds":
        sqls, schema = state.tpcds_sqls, te.TPCDS_SCHEMA
    else:
        sqls, schema = state.tpch_sqls, te.TPCH_SCHEMA
    meta, sql, _ = sqls[i]
    pre = hashlib.sha256()
    full = hashlib.sha256()
    sent = pickle.dumps(parse_one(sql))
    pre.update(sent)
    full.update(sent)
    if not meta.get("execute"):
        return pre.hexdigest(), full.hexdigest()
    expression = pickle.loads(sent)
    names = [t.name for t in te.find_tables(expression)]
    tables_ = None
    if rows:
        tables = state.tpcds_tables if name == "tpcds" else state.tpch_tables
        chosen = {n: tables[n] for n in names}
        phase and phase(True)
        tables_ = ex.ensure_tables(chosen, dialect=None)
        phase and phase(False)
    checked = ex.ensure_schema(schema, dialect=None)
    pre.update(repr(checked.supported_table_args).encode())
    if tables_ is not None and tables_.supported_table_args \
            and tables_.supported_table_args != checked.supported_table_args:
        raise ex.ExecuteError("Tables must support the same table args as schema")
    optimized = ex.optimize(expression, checked, leave_tables_isolated=True, dialect=None)
    pre.update(pickle.dumps(optimized))
    pre.update(optimized.sql(pretty=True).encode())
    plan = ex.Plan(optimized)
    pre.update(plan_digest(plan))
    duck = transpile(sql, write="duckdb")[0].encode()
    pre.update(duck)
    if not rows:
        return pre.hexdigest(), None
    phase and phase(True)
    table = ex.PythonExecutor(tables=tables_).execute(plan)
    phase and phase(False)
    full.update(pickle.dumps(table))
    full.update(duck)
    return pre.hexdigest(), full.hexdigest()
