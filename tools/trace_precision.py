"""Observe clean statement, edge and dispatch-key coverage in pytest and its workers."""

import ast
import builtins
from concurrent.futures import ProcessPoolExecutor
from functools import lru_cache
from importlib.machinery import SourceFileLoader
import json
from multiprocessing.util import Finalize
import os
from pathlib import Path
import sys
import zlib

import pytest

from checkpoint import digest
from registry_keys import assignment

ROOT = str(Path.cwd().resolve()) + "/"
OUT = Path(os.environ["PRECISION_TRACE"])
MON = sys.monitoring
TOOL = 4
EVENTS = MON.events.LINE | MON.events.BRANCH | MON.events.PY_START
before_submit = ProcessPoolExecutor.submit
before_code = SourceFileLoader.get_code
owner = "*"
items = []
records = {}
current_records = records.setdefault(owner, set())
seen_edge = {}
active = False
saved = False
code_keys = {}
instances = set()


def record(mode, source, name="", value=""):
    current_records.add((mode, source, name, value))


@lru_cache(maxsize=None)
def relative(path):
    return path[len(ROOT):] if path.startswith(ROOT + "sqlglot/") else None


def line(code, number):
    path = relative(code.co_filename)
    if path:
        record("L", path, number)
    return MON.DISABLE


def enter(code, offset):
    path = relative(code.co_filename)
    if path:
        record("F", path, code.co_firstlineno, code.co_qualname)
    return MON.DISABLE


def branch(code, source, target):
    path = relative(code.co_filename)
    if not path:
        return MON.DISABLE
    ident = id(code)
    key = ident, source
    prior = seen_edge.get(key)
    if prior == target:
        return None
    if ident not in code_keys:
        code_keys[ident] = (code, f"{path}:{code.co_firstlineno}:{code.co_qualname}")
    record("B", code_keys[ident][1], source, target)
    if prior is not None:
        return MON.DISABLE
    seen_edge[key] = target
    return None


class RecordingDict(dict):
    def observe(self, key):
        if isinstance(key, str):
            record("K", self.table_id, str(key))
        else:
            record("W", self.table_id)

    def whole(self):
        record("W", self.table_id)

    def __getitem__(self, key):
        self.observe(key)
        return super().__getitem__(key)

    def __contains__(self, key):
        self.observe(key)
        return super().__contains__(key)

    def get(self, key, default=None):
        self.observe(key)
        return super().get(key, default)

    def __iter__(self):
        self.whole()
        return super().__iter__()

    def __reversed__(self):
        self.whole()
        return super().__reversed__()

    def keys(self):
        self.whole()
        return super().keys()

    def items(self):
        self.whole()
        return super().items()

    def values(self):
        self.whole()
        return super().values()

    def copy(self):
        self.whole()
        return super().copy()

    def __or__(self, other):
        self.whole()
        return super().__or__(other)

    def __ror__(self, other):
        self.whole()
        return super().__ror__(other)


def keep_table(identifier, value):
    if type(value) is not dict:
        return value
    result = RecordingDict(value)
    result.table_id = identifier
    instances.add(identifier)
    return result


class WrapTables(ast.NodeTransformer):
    def __init__(self, path):
        self.path = path
        self.scope = ""

    def visit_ClassDef(self, node):
        scope = self.scope
        self.scope += node.name + "."
        self.generic_visit(node)
        self.scope = scope
        return node

    def visit_FunctionDef(self, node):
        return node

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Assign(self, node):
        name = assignment(node)
        if name and node.value is not None:
            value = ast.Call(func=ast.Name(id="_blast_keep_dispatch_table", ctx=ast.Load()),
                             args=[ast.Constant(self.path + ":" + self.scope + name), node.value], keywords=[])
            node.value = ast.copy_location(value, node.value)
        return node

    visit_AnnAssign = visit_Assign


def get_code(loader, fullname):
    path = relative(loader.path)
    if not path:
        return before_code(loader, fullname)
    raw = Path(loader.path).read_bytes()
    tree = ast.parse(raw, loader.path)
    return compile(ast.fix_missing_locations(WrapTables(path).visit(tree)), loader.path, "exec", dont_inherit=True)


def switch(tag):
    global owner, current_records
    owner = tag
    current_records = records.setdefault(tag, set())
    seen_edge.clear()
    MON.restart_events()


def save():
    global saved
    if saved:
        return
    saved = True
    MON.set_events(TOOL, 0)
    data = dict(items=items, records={key: list(rows) for key, rows in records.items()},
                tables=sorted(instances))
    (OUT / f"precision-{os.getpid()}.z").write_bytes(zlib.compress(json.dumps(data, separators=(",", ":")).encode(), 3))


def start():
    global active
    if active:
        return
    active = True
    builtins._blast_keep_dispatch_table = keep_table
    SourceFileLoader.get_code = get_code
    MON.use_tool_id(TOOL, "sqlglot-precision")
    MON.register_callback(TOOL, MON.events.LINE, line)
    MON.register_callback(TOOL, MON.events.BRANCH, branch)
    MON.register_callback(TOOL, MON.events.PY_START, enter)
    MON.set_events(TOOL, EVENTS)
    Finalize(None, save, exitpriority=1)


def work(tag, fn, args, kwargs):
    if not active:
        start()
    if fn.__module__ == "tests.test_executor" and fn.__name__ == "mp_execute" and args[1].get("execute"):
        MON.set_events(TOOL, 0)
        tag += "@query:" + digest(args[0])
        MON.set_events(TOOL, EVENTS)
    switch(tag)
    return fn(*args, **kwargs)


def submit(pool, fn, /, *args, **kwargs):
    return before_submit(pool, work, owner, fn, args, kwargs)


def pytest_configure(config):
    start()
    ProcessPoolExecutor.submit = submit


def pytest_collection_finish(session):
    items.extend(item.nodeid for item in session.items)


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item):
    switch(item.nodeid.split("::", 1)[0])


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_call(item):
    switch(item.nodeid)


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_teardown(item):
    switch(item.nodeid.split("::", 1)[0])


def pytest_sessionfinish(session, exitstatus):
    if exitstatus == 0:
        save()
