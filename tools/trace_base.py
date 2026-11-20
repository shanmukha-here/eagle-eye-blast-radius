"""Collect base-only function-entry and file-read contexts on CPython 3.13."""
import json
import os
import sys
import zlib
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from multiprocessing.util import Finalize
from pathlib import Path

import pytest

ROOT = str(Path.cwd()) + "/"
OUT = Path(os.environ["TRACE_OUTPUT"])
MON = sys.monitoring
TOOL = 4
GLOBAL = "*"
context = None
observations = defaultdict(set)
items = []
pool_submit = ProcessPoolExecutor.submit


def entered(code, offset):
    filename = code.co_filename
    if filename.startswith(ROOT + "sqlglot/"):
        observations[context].add((filename[len(ROOT):], code.co_firstlineno))
    return MON.DISABLE


def opened(event, args):
    if event != "open" or context is None or not isinstance(args[0], str):
        return
    # Import machinery opening a module is not a test inspecting its text.
    if sys._getframe(1).f_code.co_filename == "<frozen importlib._bootstrap_external>":
        return
    filename = os.path.abspath(args[0])
    if filename.startswith(ROOT + "tests/fixtures/") or (
        filename.startswith(ROOT + "sqlglot/") and filename.endswith(".py")
    ):
        observations[context].add((filename[len(ROOT):], 0))


def begin():
    MON.use_tool_id(TOOL, "base-coverage")
    MON.register_callback(TOOL, MON.events.PY_START, entered)
    sys.addaudithook(opened)


def switch(next_context):
    global context
    if next_context == context:
        return
    context = next_context
    MON.restart_events()
    MON.set_events(TOOL, MON.events.PY_START)


def save():
    global context
    MON.set_events(TOOL, 0)
    context = None
    payload = {"items": items,
               "hits": {key: sorted(value) for key, value in observations.items()}}
    (OUT / f"trace-{os.getpid()}.z").write_bytes(
        zlib.compress(json.dumps(payload, separators=(",", ":")).encode())
    )


def run_task(owner, fn, args, kwargs):
    if context is None:
        begin()
        Finalize(None, save, exitpriority=1)
    switch(owner)
    return fn(*args, **kwargs)


def submit(pool, fn, /, *args, **kwargs):
    return pool_submit(pool, run_task, context or GLOBAL, fn, args, kwargs)


def pytest_configure(config):
    begin()
    switch(GLOBAL)
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
