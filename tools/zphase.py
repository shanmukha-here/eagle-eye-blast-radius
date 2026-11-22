"""Code objects started while importing all of sqlglot, and while then collecting the tests.

Import-time effects of sqlglot code end in module globals and class attributes, which the
value maps digest. Effects of code run while collecting tests end in test modules, which they
do not, so the stage still declines edits to code started during collection.

usage: zphase.py ROOT TESTS_JSON OUT
"""

import json
import marshal
import os
import sys

ROOT, MANIFEST, OUT = map(os.path.abspath, sys.argv[1:4])
PREFIX = ROOT + os.sep
os.chdir(ROOT)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

mon = sys.monitoring
TOOL = 4
current = set()


def on_start(code, offset):
    f = code.co_filename
    if f.startswith(PREFIX):
        rel = f[len(PREFIX):]
        if rel.startswith(("sqlglot/", "tests/")):
            current.add((rel, code.co_qualname, code.co_firstlineno))
    return mon.DISABLE


mon.use_tool_id(TOOL, "zphase")
mon.register_callback(TOOL, mon.events.PY_START, on_start)
mon.set_events(TOOL, mon.events.PY_START)

import zrun  # noqa: E402

zrun.preload_all(ROOT)
imported = sorted(current)
current.clear()
mon.restart_events()
ids = [t["id"] for t in json.load(open(MANIFEST))["tests"]]
zrun.load(ROOT, ids)
collected = sorted(current)
with open(OUT, "wb") as f:
    marshal.dump({"import": imported, "collect": collected}, f)
print("import", len(imported), "collect", len(collected), file=sys.stderr)
