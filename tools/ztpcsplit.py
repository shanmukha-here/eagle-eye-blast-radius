"""Per-query TPC trace split at row execution: the code each query starts while executing rows,
and the digest and CPU of everything before.

See `ztpc_units.split_query`. Each query runs in a fork of the state after the executor
tests' class setup, as in ztpc.py.

usage: ztpcsplit.py ROOT OUT
"""

import marshal
import os
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
row_codes = set()
state = {"rows": False, "row_cpu": 0.0, "since": 0.0}


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
        if state["rows"]:
            row_codes.add(k)
    return mon.DISABLE


def phase(rows):
    # Re-enable every start event so code already seen before the row phase is seen again.
    now = time.process_time()
    if rows:
        state["since"] = now
    else:
        state["row_cpu"] += now - state["since"]
    state["rows"] = rows
    mon.restart_events()


mon.use_tool_id(TOOL, "ztpcsplit")
mon.register_callback(TOOL, mon.events.PY_START, on_start)
mon.set_events(TOOL, mon.events.PY_START)

import zrun  # noqa: E402
import ztpc_units  # noqa: E402

test = "tests/test_executor.py::TestExecutor::test_execute_tpcds"
found, _ = zrun.load(ROOT, [test])
klass = found[test][0]
klass.setUpClass()
result = {}
for name in ("tpcds", "tpch"):
    sqls = klass.tpcds_sqls if name == "tpcds" else klass.tpch_sqls
    units = []
    for i in range(len(sqls)):
        r, w = os.pipe()
        pid = os.fork()
        if pid == 0:
            os.close(r)
            codes.clear()
            row_codes.clear()
            mon.restart_events()
            t0 = time.process_time()
            try:
                pre, full = ztpc_units.split_query(klass, name, i, rows=True, phase=phase)
            except Exception as e:
                pre = full = "error:" + type(e).__name__
            cpu = time.process_time() - t0
            with os.fdopen(w, "wb") as f:
                f.write(marshal.dumps((sorted(row_codes), pre, full, cpu - state["row_cpu"], cpu)))
            os._exit(0)
        os.close(w)
        with os.fdopen(r, "rb") as f:
            data = f.read()
        os.waitpid(pid, 0)
        units.append(marshal.loads(data))
    result[name] = units
    print(name, len(units), file=sys.stderr)
with open(OUT, "wb") as f:
    marshal.dump(result, f)
