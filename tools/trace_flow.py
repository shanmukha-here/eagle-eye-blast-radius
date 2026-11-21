"""Record clean CPython instruction execution in test and process-pool contexts.

PYTEST_DONT_REWRITE: startup installs the monitor before pytest imports sqlglot.
"""

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

from flow import signature

ROOT = str(Path.cwd()) + "/"
MON = sys.monitoring
TOOL = 3
owner = "*"
contexts = {"*": 0}
masks = defaultdict(dict)
hits = defaultdict(set)
files = set()
opaque = set()
items = []
active = False
original_submit = ProcessPoolExecutor.submit


@lru_cache(maxsize=None)
def key(code):
    return (code.co_filename[len(ROOT):], code.co_qualname, code.co_firstlineno, signature(code))


def start(code, offset):
    if code.co_filename.startswith(ROOT + "sqlglot/"):
        files.add(code.co_filename[len(ROOT):])
        if code.co_flags & 1:
            MON.set_local_events(TOOL, code, MON.events.INSTRUCTION)
    return MON.DISABLE


def instruction(code, offset):
    hits[code].add(offset)
    return MON.DISABLE


def flush():
    bit = 1 << contexts.setdefault(owner, len(contexts))
    for code, offsets in hits.items():
        row = masks[key(code)]
        for offset in offsets:
            row[offset] = row.get(offset, 0) | bit
    hits.clear()


def switch(tag):
    global owner
    if owner != tag:
        flush()
        owner = tag
        MON.restart_events()


def audit(event, args):
    if event in ("subprocess.Popen", "os.fork", "os.forkpty", "os.posix_spawn", "os.system"):
        frame = sys._getframe(1)
        while frame:
            # The clean trace also installs in each spawned process-pool worker.
            if frame.f_globals.get("__name__") == "concurrent.futures.process" and frame.f_code.co_name == "_spawn_process":
                return
            frame = frame.f_back
        opaque.add(owner)


def save():
    MON.set_events(TOOL, 0)
    flush()
    payload = [items, contexts, sorted(files), sorted(opaque), [
        [*k, {str(o): format(v, "x") for o, v in row.items()}] for k, row in masks.items()
    ]]
    out = Path(os.environ["TRACE_OUTPUT"]) / f"flow-{os.getpid()}.z"
    out.write_bytes(zlib.compress(json.dumps(payload, separators=(",", ":")).encode(), 1))


def install():
    global active
    if active:
        return
    active = True
    MON.use_tool_id(TOOL, "base-instructions")
    MON.register_callback(TOOL, MON.events.PY_START, start)
    MON.register_callback(TOOL, MON.events.INSTRUCTION, instruction)
    MON.set_events(TOOL, MON.events.PY_START)
    sys.addaudithook(audit)
    Finalize(None, save, exitpriority=1)


def work(tag, fn, args, kwargs):
    switch(tag)
    try:
        return fn(*args, **kwargs)
    finally:
        switch("*")


def submit(pool, fn, /, *args, **kwargs):
    return original_submit(pool, work, owner, fn, args, kwargs)


def pytest_configure(config):
    install()
    ProcessPoolExecutor.submit = submit


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
        items.extend(item.nodeid for item in session.items)
    save()
