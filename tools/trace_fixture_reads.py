"""Identify which clean fixture reads discard comments and which inspect raw bytes."""

import json
import os
import sys
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from multiprocessing.util import Finalize
from pathlib import Path

import pytest

ROOT = str(Path.cwd()) + "/"
items = []
events = defaultdict(set)
owner = "*"
installed = False
worker_exit = False
submit_original = ProcessPoolExecutor.submit


def read(event, args):
    if event != "open" or not isinstance(args[0], (str, bytes)):
        return
    path = os.path.abspath(os.fsdecode(args[0]))
    if not path.startswith(ROOT + "tests/fixtures/") or not path.endswith(".sql"):
        return
    frame = sys._getframe(1)
    filtered = (frame.f_globals.get("__name__") == "tests.helpers"
                and frame.f_code.co_name in ("load_sql_fixtures", "load_sql_fixture_pairs"))
    events[path[len(ROOT):]].add("filtered" if filtered else owner)


def install():
    global installed
    if not installed:
        installed = True
        sys.addaudithook(read)


def save():
    path = Path(os.environ["TRACE_OUTPUT"]) / f"fixture-{os.getpid()}.json"
    path.write_text(json.dumps({"items": items, "events": {p: sorted(v) for p, v in events.items()}}))


def work(context, fn, args, kwargs):
    global owner, worker_exit
    install()
    if not worker_exit:
        worker_exit = True
        Finalize(None, save, exitpriority=1)
    owner = context
    return fn(*args, **kwargs)


def submit(pool, fn, /, *args, **kwargs):
    return submit_original(pool, work, owner, fn, args, kwargs)


def pytest_configure(config):
    install()
    ProcessPoolExecutor.submit = submit


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item):
    global owner
    owner = item.nodeid.split("::", 1)[0]


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_call(item):
    global owner
    owner = item.nodeid


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_teardown(item, nextitem):
    global owner
    owner = item.nodeid.split("::", 1)[0]


def pytest_sessionfinish(session, exitstatus):
    if not exitstatus:
        items.extend(item.nodeid for item in session.items)
        save()
