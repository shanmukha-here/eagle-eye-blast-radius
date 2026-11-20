"""Record operand types while retaining Python's original comparison evaluation.

PYTEST_DONT_REWRITE: this profiler hooks imports before pytest initializes.
"""

import ast
import builtins
import functools
import json
import os
import zlib
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from enum import Enum
from importlib.machinery import SourceFileLoader
from multiprocessing.util import Finalize
from pathlib import Path

import pytest

ROOT = str(Path.cwd()) + "/"
owner = "*"
samples = defaultdict(set)
items = []
submit_original = ProcessPoolExecutor.submit
source_loader = SourceFileLoader.get_code
installed = False


@functools.lru_cache(maxsize=None)
def describe(cls):
    if cls.__module__ == "builtins" and cls in (
        type(None),
        bool,
        int,
        float,
        complex,
        str,
        bytes,
        list,
        tuple,
        dict,
        set,
        frozenset,
        object,
    ):
        return "builtin:" + cls.__name__
    if issubclass(cls, Enum):
        eq, ne = cls.__eq__, cls.__ne__
        safe = eq is object.__eq__ and ne is object.__ne__
        for primitive in (str, int):
            if eq is primitive.__eq__ and ne is primitive.__ne__:
                members = list(cls.__members__.values())
                safe = all(
                    bool(eq(x, y)) == (x is y) and bool(ne(x, y)) == (x is not y)
                    for x in members
                    for y in members
                )
        if safe:
            return "enum:" + cls.__module__ + "." + cls.__qualname__
    if cls.__eq__ is object.__eq__ and cls.__ne__ is object.__ne__:
        return "identity"
    return "unresolved"


@functools.lru_cache(maxsize=None)
def members(cls):
    return frozenset(id(member) for member in cls.__members__.values())


def observe(key, value):
    cls = type(value)
    kind = describe(cls)
    if kind.startswith("enum:") and id(value) not in members(cls):
        kind = "unresolved"
    samples[owner].add((key, kind))
    return value


class Wrap(ast.NodeTransformer):
    def __init__(self, path):
        self.path = path

    def visit_Compare(self, node):
        self.generic_visit(node)
        if len(node.ops) != 1:
            return node
        left, right = node.left, node.comparators[0]
        if not any(
            isinstance(x, ast.Constant)
            and x.value is None
            or isinstance(x, ast.Attribute)
            and x.attr.isupper()
            for x in (left, right)
        ):
            return node
        args = []
        for side, expr in enumerate((left, right)):
            if isinstance(expr, ast.Constant):
                args.append(expr)
                continue
            key = f"{self.path}:{node.lineno}:{node.col_offset}:{side}"
            call = ast.Call(
                func=ast.Name(id="_record_base_operand_type", ctx=ast.Load()),
                args=[ast.Constant(key), expr],
                keywords=[],
            )
            args.append(ast.copy_location(call, expr))
        node.left, node.comparators[0] = args
        return node


def get_code(loader, fullname):
    f = loader.get_filename(fullname)
    if not f.startswith(ROOT + "sqlglot/"):
        return source_loader(loader, fullname)
    tree = Wrap(f[len(ROOT) :]).visit(ast.parse(Path(f).read_bytes(), filename=f))
    return compile(ast.fix_missing_locations(tree), f, "exec", dont_inherit=True)


def save():
    out = Path(os.environ["TRACE_OUTPUT"])
    data = [items, {tag: sorted(records) for tag, records in samples.items()}]
    (out / f"operands-{os.getpid()}.z").write_bytes(
        zlib.compress(json.dumps(data).encode())
    )


def install():
    global installed
    if installed:
        return
    installed = True
    builtins._record_base_operand_type = observe
    SourceFileLoader.get_code = get_code
    Finalize(None, save, exitpriority=1)


def work(tag, fn, args, kwargs):
    global owner
    owner = tag
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
    if exitstatus == 0:
        items.extend(t.nodeid for t in session.items)
    save()
