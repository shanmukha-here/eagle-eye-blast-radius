"""Trace, per test, the class attributes and module attributes of sqlglot it reads.

After importing all of sqlglot, every class attribute that is not itself a descriptor is
replaced by a non-data descriptor that records its (file, class, name) and returns the value.
Lookups on the class, its subclasses and their instances, through super() and getattr all
pass through it. Each sqlglot module is given a module subclass whose __getattribute__
records (file, name), which sees `module.name` and `from module import name`. Reads by a
module's own functions (LOAD_GLOBAL) are not seen here; the stage takes them from the code
objects the test started and their names.

Records reads during collection under "<collect>", and for each test its reads and whether it
failed, so tests the wrappers changed can be excluded.

usage: zattr.py ROOT TESTS_JSON OUT SHARD NSHARDS   (NSHARDS 0: one sequential run)
"""

import enum
import json
import marshal
import os
import sys
import time
import types

ROOT, MANIFEST, OUT, SHARD, NSHARDS = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4]), int(sys.argv[5])
ROOT, MANIFEST, OUT = map(os.path.abspath, (ROOT, MANIFEST, OUT))
PREFIX = os.path.join(ROOT, "sqlglot") + os.sep
os.chdir(ROOT)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import zrun  # noqa: E402

reads = set()
SKIP = {"__module__", "__qualname__", "__doc__", "__dict__", "__weakref__", "__firstlineno__",
        "__static_attributes__"}


class Tracked:
    __slots__ = ("value", "key")

    def __init__(self, value, key):
        self.value = value
        self.key = key

    def __get__(self, obj, owner=None):
        reads.add(self.key)
        return self.value


module_keys = {}


class TrackedModule(types.ModuleType):
    def __getattribute__(self, name):
        rel = module_keys.get(id(self))
        if rel is not None:
            reads.add(("m", rel, name))
        return types.ModuleType.__getattribute__(self, name)


zrun.preload_all(ROOT)
tracked = []
seen = set()
for module in list(sys.modules.values()):
    path = getattr(module, "__file__", None) or ""
    if not path.startswith(PREFIX):
        continue
    rel = path[len(ROOT) + 1:]
    stack = [v for v in vars(module).values() if isinstance(v, type)]
    while stack:
        cls = stack.pop()
        if id(cls) in seen or getattr(sys.modules.get(cls.__module__), "__file__", None) != path \
                or "<locals>" in cls.__qualname__:
            continue
        seen.add(id(cls))
        values = list(vars(cls).items())
        stack.extend(v for _, v in values if isinstance(v, type))
        if issubclass(cls, enum.Enum):
            continue
        for name, v in values:
            if name in SKIP or (name.startswith("__") and name.endswith("__")):
                continue
            if hasattr(type(v), "__get__"):
                continue
            key = ("c", rel, cls.__qualname__, name)
            type.__setattr__(cls, name, Tracked(v, key))
            tracked.append(key)
    if type(module) is types.ModuleType:
        module_keys[id(module)] = rel
        module.__class__ = TrackedModule

tests = json.load(open(MANIFEST))["tests"]
ids = [t["id"] for t in tests]
found, broken = zrun.load(ROOT, ids)
collect = sorted(reads)
mine = [t for i, t in enumerate(ids) if NSHARDS and i % NSHARDS == SHARD]
records = {"<collect>": collect, "<broken>": sorted(broken), "<tracked>": tracked if SHARD == 0 else None}
runner = zrun.Runner(found)
if NSHARDS == 0:
    # Sequential: every test in manifest order in this interpreter, as one pytest process.
    for test_id in ids:
        if test_id not in found:
            records[test_id] = None
            continue
        reads.clear()
        t0 = time.process_time()
        failed, done = runner.run([test_id], {test_id: 900})
        records[test_id] = {"reads": sorted(reads), "failed": test_id in failed, "cpu": time.process_time() - t0}
    records["<tracked>"] = tracked
    mine = []
for test_id in mine:
    if test_id not in found:
        records[test_id] = None
        continue
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(r)
        reads.clear()
        t0 = time.process_time()
        failed, done = runner.run([test_id], {test_id: 900})
        data = marshal.dumps([sorted(reads), test_id in failed, time.process_time() - t0])
        with os.fdopen(w, "wb") as f:
            f.write(data)
        os._exit(0)
    os.close(w)
    with os.fdopen(r, "rb") as f:
        data = f.read()
    os.waitpid(pid, 0)
    if not data:
        records[test_id] = "crash"
        continue
    got, fail, cpu = marshal.loads(data)
    records[test_id] = {"reads": got, "failed": fail, "cpu": cpu}
os.makedirs(OUT, exist_ok=True)
with open(os.path.join(OUT, "shard-%03d.marshal" % SHARD), "wb") as f:
    marshal.dump(records, f)
print("shard", SHARD, "done", len(mine), file=sys.stderr)
