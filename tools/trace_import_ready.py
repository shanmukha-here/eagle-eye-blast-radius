"""Record whether a guarded import would find its module already initialized."""

import ast
import json
import os
import sys
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from functools import lru_cache
from multiprocessing.util import Finalize
from pathlib import Path

import pytest

ROOT = str(Path.cwd()) + "/"
TOOL = 5
MON = sys.monitoring
cases = {}
observations = {}
items = []
installed = False
child_registered = False
old_submit = ProcessPoolExecutor.submit


@lru_cache(maxsize=None)
def guards(filename):
    tree = ast.parse(Path(filename).read_bytes())
    output = {}
    aliases = {name.asname or name.name for node in tree.body if isinstance(node, ast.Import)
               for name in node.names if name.name == "typing"}
    for node in tree.body:
        if not (isinstance(node, ast.If) and not node.orelse
                and isinstance(node.test, ast.Attribute) and node.test.attr == "TYPE_CHECKING"
                and isinstance(node.test.value, ast.Name) and node.test.value.id in aliases):
            continue
        if any(not isinstance(stmt, ast.ImportFrom) or stmt.level or not stmt.module
               or any(alias.name == "*" for alias in stmt.names) for stmt in node.body):
            continue
        output[node.lineno] = tuple((stmt.module, tuple(a.name for a in stmt.names))
                                    for stmt in node.body)
    return output


def entered(code, offset):
    filename = code.co_filename
    if code.co_name == "<module>" and filename.startswith(ROOT + "sqlglot/"):
        sites = guards(filename)
        if sites:
            cases[code] = sites
            MON.set_local_events(TOOL, code, MON.events.LINE)
    return MON.DISABLE


def line(code, number):
    imports = cases[code].get(number)
    if not imports:
        return MON.DISABLE
    ready = True
    for module, names in imports:
        value = sys.modules.get(module)
        if (value is None or getattr(value.__spec__, "_initializing", True)
                or not set(names) <= vars(value).keys()):
            ready = False
            break
    key = f"{code.co_filename[len(ROOT):]}:{number}"
    observations[key] = observations.get(key, True) and ready


def install():
    global installed
    if not installed:
        installed = True
        MON.use_tool_id(TOOL, "ready-imports")
        MON.register_callback(TOOL, MON.events.PY_START, entered)
        MON.register_callback(TOOL, MON.events.LINE, line)
        MON.set_events(TOOL, MON.events.PY_START)


def save():
    output = Path(os.environ["TRACE_OUTPUT"]) / f"ready-{os.getpid()}.json"
    output.write_text(json.dumps({"items": items, "observations": observations}))


def task(fn, args, kwargs):
    global child_registered
    install()
    if not child_registered:
        child_registered = True
        Finalize(None, save, exitpriority=1)
    return fn(*args, **kwargs)


def submit(pool, fn, /, *args, **kwargs):
    return old_submit(pool, task, fn, args, kwargs)


def pytest_configure(config):
    install()
    ProcessPoolExecutor.submit = submit


def pytest_sessionfinish(session, exitstatus):
    if exitstatus == 0:
        items.extend(t.nodeid for t in session.items)
        save()
