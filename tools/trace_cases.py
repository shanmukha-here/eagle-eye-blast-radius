"""Record clean query-level coverage inside the real upstream executor test.

PYTEST_DONT_REWRITE: startup installs the monitor before pytest imports sqlglot.
"""

import functools
import json
import os
import sys
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from importlib.machinery import SourceFileLoader
from multiprocessing.util import Finalize
from pathlib import Path

import pytest

from checkpoint import digest, table_digest

ROOT = str(Path.cwd()) + "/"
MON = sys.monitoring
TOOL = 4
normal_exec = SourceFileLoader.exec_module
normal_submit = ProcessPoolExecutor.submit
coverage = defaultdict(set)
answers = defaultdict(set)
optimized = defaultdict(set)
tests = []
context = "outside"
active = False


def entered(code, offset):
    if code.co_filename.startswith(ROOT + "sqlglot/"):
        key = f"{code.co_filename[len(ROOT):]}:{code.co_firstlineno}:{code.co_qualname}"
        coverage[key].add(context)
    return MON.DISABLE


def switch(value):
    global context
    context = value
    MON.restart_events()


def wrap(fn):
    @functools.wraps(fn)
    def observe(*args, **kwargs):
        caller = context
        frame = sys._getframe(1)
        if not (caller.startswith("query:") and frame.f_globals.get("__name__") == "sqlglot.executor"
                and frame.f_code.co_name == "execute"):
            return fn(*args, **kwargs)
        key = caller.split(":", 1)[1]
        switch("opt:" + key)
        try:
            result = fn(*args, **kwargs)
        finally:
            switch("outside")
        try:
            optimized[key].add(digest(args[0], result))
        except Exception:
            optimized[key].add("unknown")
        finally:
            switch(caller)
        return result
    return observe


def exec_module(loader, module):
    normal_exec(loader, module)
    if module.__name__ == "sqlglot.optimizer.optimizer":
        module.optimize = wrap(module.optimize)


def work(fn, args, kwargs):
    if not args[1].get("execute"):
        return fn(*args, **kwargs)
    key = digest(args[0])
    switch("query:" + key)
    try:
        table = fn(*args, **kwargs)
    finally:
        switch("outside")
    answers[key].add(table_digest(table))
    return table


def submit(pool, fn, /, *args, **kwargs):
    if fn.__module__ == "tests.test_executor" and fn.__name__ == "mp_execute":
        return normal_submit(pool, work, fn, args, kwargs)
    return normal_submit(pool, fn, *args, **kwargs)


def save():
    MON.set_events(TOOL, 0)
    payload = [tests, *({k: sorted(v) for k, v in d.items()} for d in (coverage, answers, optimized))]
    (Path(os.environ["PROBE_OUTPUT"]) / f"case-{os.getpid()}.json").write_text(json.dumps(payload))


def install():
    global active
    if active:
        return
    active = True
    MON.use_tool_id(TOOL, "executor-call-coverage")
    MON.register_callback(TOOL, MON.events.PY_START, entered)
    MON.set_events(TOOL, MON.events.PY_START)
    SourceFileLoader.exec_module = exec_module
    Finalize(None, save, exitpriority=1)


def pytest_configure(config):
    install()
    ProcessPoolExecutor.submit = submit


@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session, exitstatus):
    if exitstatus == 0:
        tests.extend(item.nodeid for item in session.items)
    save()
