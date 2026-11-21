"""Record pinned TPC-DS execution and DuckDB rendering boundaries."""

from collections import defaultdict
from pathlib import Path
import pickle
import sys

from precision_index import build
from tpcds_render import fingerprint


def main(base, output):
    prefix = str(base.resolve()) + "/"
    index = defaultdict(int)
    monitor = sys.monitoring
    tool = 4
    stage = "downstream:"
    mask = 1

    def observe(code, *event):
        filename = code.co_filename
        if filename.startswith(prefix + "sqlglot/"):
            index[stage + filename[len(prefix):]] |= mask
        return monitor.DISABLE

    monitor.use_tool_id(tool, "TPC-DS execution and rendering")
    for event in (monitor.events.PY_START, monitor.events.PY_RESUME):
        monitor.register_callback(tool, event, observe)
    monitor.set_events(tool, monitor.events.PY_START | monitor.events.PY_RESUME)
    try:
        from sqlglot import parse_one, transpile
        from tests.test_executor import TPCDS_SCHEMA, TestExecutor, initializer, mp_execute

        test = TestExecutor("test_execute_tpcds")
        test.setUpClass()
        try:
            initializer(TPCDS_SCHEMA, test.tpcds_tables)
            render = []
            for number, (meta, sql, _) in enumerate(test.tpcds_sqls):
                tree, data = pickle.loads(pickle.dumps((parse_one(sql), meta)))
                result = mp_execute(tree, data)
                if result is not None:
                    render.append((number, sql))
            if len(render) != sum(bool(meta.get("execute")) for meta, _, _ in test.tpcds_sqls):
                raise ValueError("Incomplete TPC-DS execution boundary")
            index["count"] = len(test.tpcds_sqls)
        finally:
            test.tearDownClass()
        for module in tuple(sys.modules.values()):
            filename = vars(module).get("__file__") if module is not None else None
            if filename and filename.startswith(prefix + "sqlglot/"):
                index["loaded:" + filename[len(prefix):]] = 1

        stage = "render:"
        for number, sql in render:
            mask = 1 << number
            monitor.restart_events()
            rendered = transpile(sql, write="duckdb")
            if len(rendered) != 1:
                raise ValueError("Unexpected multi-statement TPC-DS query")
            index[f"sql:{number}"] = fingerprint(sql)
            index[f"rendered:{number}"] = fingerprint(rendered[0])
    finally:
        monitor.set_events(tool, 0)
        monitor.free_tool_id(tool)
    build(output, index, 32)
    print(f"Indexed TPC-DS reference renders for {len(render)} queries", flush=True)


if __name__ == "__main__":
    main(*(Path(v) for v in sys.argv[1:]))
