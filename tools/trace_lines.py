"""Profile executed lines, resolved dialects and source-position observers on the base."""

import json
import os
import sys
import zlib
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from functools import lru_cache
from multiprocessing.util import Finalize
from pathlib import Path

import pytest

ROOT = str(Path.cwd()) + "/"
OUT = Path(os.environ["TRACE_OUTPUT"])
MON = sys.monitoring
TOOL = 4
context = None
contexts = {}
items = []
hits = defaultdict(set)
maps = defaultdict(dict)
normal_submit = ProcessPoolExecutor.submit
dialect_file = ROOT + "sqlglot/dialects/dialect.py"


@lru_cache(maxsize=None)
def relative(filename):
    if filename.startswith((ROOT + "sqlglot/", ROOT + "tests/")):
        return filename[len(ROOT) :]
    return None


def line_seen(code, line):
    filename = relative(code.co_filename)
    if filename:
        hits[filename].add(line)
    return MON.DISABLE


def entered(code, offset):
    if code.co_filename == dialect_file and code.co_name == "get_or_raise":
        MON.set_local_events(TOOL, code, MON.events.PY_RETURN)
    return MON.DISABLE


@lru_cache(maxsize=256)
def dialect_sources(instance_type):
    return tuple(
        cls.__module__.replace(".", "/") + ".py"
        for cls in instance_type.__mro__
        if cls.__module__.startswith("sqlglot.dialects.")
    )


def returned(code, offset, result):
    if context is not None:
        for filename in dialect_sources(type(result)):
            hits["@dialect"].add(filename)


def flush():
    if context is None:
        return
    bit = 1 << contexts.setdefault(context, len(contexts))
    for filename, keys in hits.items():
        rows = maps[filename]
        for key in keys:
            rows[key] = rows.get(key, 0) | bit
    hits.clear()


def switch(owner):
    global context
    if owner != context:
        flush()
        context = owner
        MON.restart_events()
        MON.set_events(TOOL, MON.events.LINE | MON.events.PY_START)


def begin():
    MON.use_tool_id(TOOL, "base-lines")
    MON.register_callback(TOOL, MON.events.LINE, line_seen)
    MON.register_callback(TOOL, MON.events.PY_START, entered)
    MON.register_callback(TOOL, MON.events.PY_RETURN, returned)


def save():
    global context
    MON.set_events(TOOL, 0)
    flush()
    context = None
    data = [
        items,
        contexts,
        {f: {k: format(v, "x") for k, v in rows.items()} for f, rows in maps.items()},
    ]
    (OUT / f"detail-{os.getpid()}.z").write_bytes(
        zlib.compress(json.dumps(data).encode())
    )


def work(owner, fn, args, kwargs):
    if context is None:
        begin()
        Finalize(None, save, exitpriority=1)
    switch(owner)
    return fn(*args, **kwargs)


def submit(pool, fn, /, *args, **kwargs):
    return normal_submit(pool, work, context or "*", fn, args, kwargs)


def pytest_configure(config):
    begin()
    switch("*")
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
def pytest_runtest_teardown(item, nextitem):
    switch(item.nodeid.split("::", 1)[0])


def pytest_sessionfinish(session, exitstatus):
    if exitstatus == 0:
        save()
