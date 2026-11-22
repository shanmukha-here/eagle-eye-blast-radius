"""Trace fixture-driven tests case by case on the pinned source.

The SQL fixture loaders in tests/helpers.py are wrapped. While a test's loop body handles the
item a loader just yielded, or runs under a subTest whose title names that item, the code
objects and lines it starts belong to that case. Everything else in the test is "outside".
A loader that is exhausted, or consumed without a loop body (list()), leaves later work
outside. The stage may drop such a test only when changed code ran in cases alone, after
running those cases (and the whole outside part) on the patched code.

Records, per test that reads a fixture: for each case (file, index) its code keys, lines and
CPU, the same for outside, and whether the test failed.

usage: zcase.py ROOT TESTS_JSON OUT SHARD NSHARDS
"""

import json
import marshal
import os
import sys
import time
import types

ROOT, MANIFEST, OUT, SHARD, NSHARDS = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4]), int(sys.argv[5])
ROOT, MANIFEST, OUT = map(os.path.abspath, (ROOT, MANIFEST, OUT))
PREFIX = ROOT + os.sep
os.chdir(ROOT)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# ZCASE_REVERSE=1 yields every fixture's items in reverse order. Code whose owning cases differ
# between the two orders depends on earlier cases (lazy setup, caches) and is not case-local.
REVERSE = os.environ.get("ZCASE_REVERSE") == "1"
MODULES = ("tests/test_optimizer.py", "tests/test_transpile.py", "tests/test_serde.py",
           "tests/test_executor.py")

mon = sys.monitoring
TOOL = 4
keys = {}
state = {"case": None, "since": 0.0, "used": False}
codes, lines = set(), set()
records = {}


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


def on_line(code, line):
    k = key(code)
    if k is not None:
        lines.add((k, line))
    return mon.DISABLE


def switch(case):
    """Close the current owner's sets and start a new owner."""
    now = time.process_time()
    owner = state["case"]
    rec = records.setdefault(owner, [set(), set(), 0.0])
    rec[0] |= codes
    rec[1] |= lines
    rec[2] += now - state["since"]
    codes.clear()
    lines.clear()
    state["case"] = case
    state["since"] = time.process_time()
    mon.restart_events()


import zrun  # noqa: E402

tests = json.load(open(MANIFEST))["tests"]
ids = [t["id"] for t in tests]
mine = [t for i, t in enumerate(ids) if i % NSHARDS == SHARD and t.split("::")[0] in MODULES
        and os.environ.get("ZCASE_ONLY", "") in t]
found, broken = zrun.load(ROOT, mine)

import unittest  # noqa: E402

titles = {}


def wrap(orig, pairs):
    def loader(filename, *a, **k):
        state["used"] = True
        items = enumerate(orig(filename, *a, **k))
        if REVERSE:
            items = reversed(list(items))
        for p, (i, item) in enumerate(items):
            case = (filename, i)
            if pairs:
                meta, sql, _ = item
                # Consumers number items as they receive them (enumerate(..., start=1)).
                for title in (meta.get("title"), f"{p + 1}, {sql}"):
                    if title:
                        titles.setdefault(title, set()).add(case)
            switch(case)
            yield item
        switch(None)
    return loader


orig_subtest = unittest.TestCase.subTest


def subtest(self, msg=None, **params):
    import contextlib
    title = msg[len("(execute) "):] if isinstance(msg, str) and msg.startswith("(execute) ") else msg
    cases = titles.get(title) if isinstance(title, str) else None

    @contextlib.contextmanager
    def body():
        if not cases or len(cases) != 1:
            with orig_subtest(self, msg, **params):
                yield
            return
        before = state["case"]
        switch(next(iter(cases)))
        try:
            with orig_subtest(self, msg, **params):
                yield
        finally:
            switch(before)
    return body()


HELPERS = os.path.join(ROOT, "tests", "helpers.py")
wrapped = {}
for module in list(sys.modules.values()):
    for name, value in list(vars(module).items()) if module else ():
        code = getattr(value, "__code__", None) if isinstance(value, types.FunctionType) else None
        if code is None or code.co_filename != HELPERS \
                or value.__name__ not in ("load_sql_fixture_pairs", "load_sql_fixtures"):
            continue
        if value not in wrapped:
            wrapped[value] = wrap(value, value.__name__ == "load_sql_fixture_pairs")
        setattr(module, name, wrapped[value])
print("wrapped loaders", len(wrapped), file=sys.stderr)
unittest.TestCase.subTest = subtest

mon.use_tool_id(TOOL, "zcase")
mon.register_callback(TOOL, mon.events.PY_START, on_start)
mon.register_callback(TOOL, mon.events.LINE, on_line)
runner = zrun.Runner(found)
out = {}
for test_id in mine:
    if test_id not in found:
        continue
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(r)
        records.clear(); titles.clear(); codes.clear(); lines.clear()
        state.update(case=None, since=time.process_time(), used=False)
        mon.set_events(TOOL, mon.events.PY_START | mon.events.LINE)
        mon.restart_events()
        failed, done = runner.run([test_id], {test_id: 900})
        switch(None)
        mon.set_events(TOOL, 0)
        data = None
        if state["used"]:
            data = {"failed": test_id in failed,
                    "cases": {c: (sorted(v[0]), sorted(v[1]), v[2]) for c, v in records.items()}}
        with os.fdopen(w, "wb") as f:
            f.write(marshal.dumps(data))
        os._exit(0)
    os.close(w)
    with os.fdopen(r, "rb") as f:
        data = f.read()
    os.waitpid(pid, 0)
    if data:
        rec = marshal.loads(data)
        if rec is not None:
            out[test_id] = rec
os.makedirs(OUT, exist_ok=True)
with open(os.path.join(OUT, "shard-%03d.marshal" % SHARD), "wb") as f:
    marshal.dump(out, f)
print("shard", SHARD, "tests", len(mine), "fixture tests", len(out), file=sys.stderr)
