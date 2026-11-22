"""Trace every numbered test in manifest order in one interpreter, as one pytest process runs them.

Records, per test: executed code objects and lines under sqlglot/ and tests/, files opened
under the source root, whether it starts processes, its CPU time and whether it failed.

usage: ztrace.py ROOT TESTS_JSON OUT SHARD NSHARDS
"""

import json
import marshal
import os
import sys
import time

ROOT, MANIFEST, OUT, SHARD, NSHARDS = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4]), int(sys.argv[5])
ROOT, MANIFEST, OUT = map(os.path.abspath, (ROOT, MANIFEST, OUT))
PREFIX = ROOT + os.sep
os.chdir(ROOT)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

mon = sys.monitoring
TOOL = 4
keys = {}
codes = set()
lines = set()
opened = set()
state = {"spawn": False}


def key(code):
    k = keys.get(code)
    if k is None:
        f = code.co_filename
        if f.startswith(PREFIX):
            f = f[len(PREFIX):]
            if not (f.startswith("sqlglot/") or f.startswith("tests/")):
                f = None
        else:
            f = None
        k = keys[code] = None if f is None else (f, code.co_qualname, code.co_firstlineno)
    return k


def on_start(code, offset):
    k = key(code)
    if k is not None:
        codes.add(k)
    return mon.DISABLE


def on_line(code, line):
    k = key(code)
    if k is not None:
        lines.add((k, line))
    return mon.DISABLE


def audit(event, args):
    if event == "open":
        path = args[0]
        if isinstance(path, str):
            path = os.path.abspath(path)
            if path.startswith(PREFIX):
                opened.add(path[len(PREFIX):])
    elif event in ("subprocess.Popen", "os.posix_spawn", "os.exec", "os.spawn"):
        state["spawn"] = True


sys.addaudithook(audit)
import multiprocessing.process

_start = multiprocessing.process.BaseProcess.start


def start(self, *a, **k):
    state["spawn"] = True
    return _start(self, *a, **k)


multiprocessing.process.BaseProcess.start = start

mon.use_tool_id(TOOL, "ztrace")
mon.register_callback(TOOL, mon.events.PY_START, on_start)
mon.register_callback(TOOL, mon.events.LINE, on_line)
mon.set_events(TOOL, mon.events.PY_START | mon.events.LINE)

import zrun

tests = json.load(open(MANIFEST))["tests"]
ids = [t["id"] for t in tests]
preload_failed = []
found, broken = zrun.load(ROOT, ids)


def snapshot():
    c = sorted(codes)
    lines_by = {}
    for k, line in lines:
        lines_by.setdefault(k, []).append(line)
    return c, {k: sorted(v) for k, v in lines_by.items()}, sorted(opened), state["spawn"]


os.makedirs(OUT, exist_ok=True)
mon.set_events(TOOL, mon.events.PY_START)
collect = snapshot()
records = {"<collect>": collect[:3], "<broken>": sorted(broken)}
runner = zrun.Runner(found)
for test_id in ids:
    if test_id not in found:
        records[test_id] = None
        continue
    codes.clear(); lines.clear(); opened.clear(); state["spawn"] = False
    mon.restart_events()
    t0 = time.process_time()
    failed, done = runner.run([test_id], {test_id: 600})
    c, lb, op, sp = snapshot()
    records[test_id] = {"codes": c, "lines": {}, "opens": op, "spawn": sp, "failed": test_id in failed,
                        "cpu": time.process_time() - t0, "wall": 0.0, "child_cpu": 0.0}
with open(os.path.join(OUT, "shard-000.marshal"), "wb") as f:
    marshal.dump(records, f)
print("sequential done", len(records), file=sys.stderr)
