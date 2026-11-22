"""Per-query units of the TPC executor tests on the pinned source: coverage and result digests.

The executor tests parse each query, execute it with sqlglot's Python executor in a process
pool, and compare against DuckDB running sqlglot's DuckDB transpilation. Each query is split
into the same three calls. A query whose calls return the same digests as on the pin
compares the same way.

usage: ztpc.py ROOT OUT
"""

import hashlib
import marshal
import os
import pickle
import sys
import time

ROOT, OUT = map(os.path.abspath, sys.argv[1:3])
os.chdir(ROOT)
sys.path.insert(0, ROOT)
sys.path.insert(1, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PREFIX = ROOT + os.sep

mon = sys.monitoring
TOOL = 4
keys = {}
codes = set()


def key(code):
    k = keys.get(code)
    if k is None:
        f = code.co_filename
        k = None
        if f.startswith(PREFIX) and f[len(PREFIX):].startswith(("sqlglot/", "tests/")):
            k = (f[len(PREFIX):], code.co_qualname, code.co_firstlineno)
        keys[code] = k
    return k


def on_start(code, offset):
    k = key(code)
    if k is not None:
        codes.add(k)
    return mon.DISABLE


lines = set()


def on_line(code, line):
    k = key(code)
    if k is not None and k[0].startswith("sqlglot/"):
        lines.add((k[0], line))
    return mon.DISABLE


mon.use_tool_id(TOOL, "ztpc")
mon.register_callback(TOOL, mon.events.PY_START, on_start)
mon.register_callback(TOOL, mon.events.LINE, on_line)
mon.set_events(TOOL, mon.events.PY_START | mon.events.LINE)

import zrun  # noqa: E402
import ztpc_units  # noqa: E402

tests = {"tpcds": "tests/test_executor.py::TestExecutor::test_execute_tpcds",
         "tpch": "tests/test_executor.py::TestExecutor::test_execute_tpch"}
found, _ = zrun.load(ROOT, list(tests.values()))
collect = sorted(codes)
codes.clear()
lines.clear()
mon.restart_events()
klass = found[tests["tpcds"]][0]
klass.setUpClass()
setup = sorted(codes)
result = {"collect": collect, "setup": setup, "units": {}}
for name in ("tpcds", "tpch"):
    sqls = klass.tpcds_sqls if name == "tpcds" else klass.tpch_sqls
    units = []
    for i, (meta, sql, _) in enumerate(sqls):
        # Each query starts from the same state, so its coverage includes any cache it fills.
        r, w = os.pipe()
        pid = os.fork()
        if pid == 0:
            os.close(r)
            codes.clear()
            lines.clear()
            mon.restart_events()
            t0 = time.process_time()
            digest = ztpc_units.run_query(klass, name, i)
            cpu = time.process_time() - t0
            with os.fdopen(w, "wb") as f:
                f.write(marshal.dumps((sorted(codes), digest, cpu, sorted(lines))))
            os._exit(0)
        os.close(w)
        with os.fdopen(r, "rb") as f:
            data = f.read()
        os.waitpid(pid, 0)
        units.append(marshal.loads(data))
    result["units"][name] = units
    print(name, len(units), file=sys.stderr)
with open(OUT, "wb") as f:
    marshal.dump(result, f)
