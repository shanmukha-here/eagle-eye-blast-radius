"""Measure concrete receiver classes, including inherited classes, on the untouched pin."""

import functools
import json
import os
import zlib
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from multiprocessing.util import Finalize
from pathlib import Path

import pytest

owner = "*"
seen = defaultdict(set)
installed = False
items = []
original_submit = ProcessPoolExecutor.submit


def record_constructor(cls):
    original = cls.__init__

    @functools.wraps(original)
    def observed(self, *args, **kwargs):
        seen[owner].add(type(self))
        return original(self, *args, **kwargs)

    cls.__init__ = observed


def install():
    global installed
    if installed:
        return
    installed = True
    from sqlglot.dialects.dialect import Dialect
    from sqlglot.expressions.core import Expr
    from sqlglot.parser import Parser
    from sqlglot.generator import Generator
    from sqlglot.tokens import Tokenizer

    for cls in (Dialect, Expr, Parser, Generator, Tokenizer):
        record_constructor(cls)


def save():
    data = {}
    for tag, types in seen.items():
        names = set()
        for t in types:
            for cls in t.__mro__:
                if cls.__module__.startswith("sqlglot."):
                    names.add(
                        cls.__module__.replace(".", "/") + ".py:" + cls.__qualname__
                    )
        data[tag] = sorted(names)
    out = Path(os.environ["TRACE_OUTPUT"])
    (out / f"types-{os.getpid()}.z").write_bytes(
        zlib.compress(json.dumps([items, data]).encode())
    )


def work(tag, fn, args, kwargs):
    global owner
    if not installed:
        install()
        Finalize(None, save, exitpriority=1)
    owner = tag
    return fn(*args, **kwargs)


def submit(pool, fn, /, *args, **kwargs):
    return original_submit(pool, work, owner, fn, args, kwargs)


def pytest_configure(config):
    install()
    ProcessPoolExecutor.submit = submit


def pytest_collection_finish(session):
    items.extend(t.nodeid for t in session.items)


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
    if exitstatus == 0:
        save()
