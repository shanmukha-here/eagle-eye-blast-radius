"""Record clean Python code entries between upstream assertion boundaries."""
import ast
import builtins
from collections import defaultdict
from contextlib import contextmanager
import base64
import functools
from importlib.machinery import SourceFileLoader
import json
import marshal
import os
from pathlib import Path
import random
import sys
import time
import unittest
import warnings

import pytest

from assertion_units import HELPERS, admissible, error_context, literals, sql_assertion, uses_local_names

root = str(Path.cwd().resolve()) + "/"
monitor = sys.monitoring
tool = 3
active = None
frames = []
objects = {}
code_ids = {}
originals = []
opaque = defaultdict(set)
startup = set()
normal_code = SourceFileLoader.get_code


@functools.lru_cache(maxsize=4096)
def receiver_scopes(cls):
    result = []
    for parent in cls.__mro__:
        module = sys.modules.get(parent.__module__)
        path = getattr(module, "__file__", None) if module else None
        if isinstance(path, str) and path.startswith(root + "sqlglot/"):
            result.append((path[len(root):], -1, parent.__qualname__))
    return tuple(result)


def direct(recipe, names, call):
    if active is None:
        return call()
    local = sys._getframe(1).f_locals
    args = {name: local[name] for name in names if name in local}
    payload = ["direct", recipe, args] if len(args) == len(names) and literals(args) else ["opaque", "assertion"]
    with unit(payload):
        return call()


@contextmanager
def direct_block(recipe, names, local):
    if active is None:
        yield
        return
    args = {name: local[name] for name in names if name in local}
    payload = ["block", recipe, args] if len(args) == len(names) and literals(args) else ["opaque", "assertion"]
    with unit(payload):
        yield


class Instrument(ast.NodeTransformer):
    def visit_With(self, node):
        names = sorted(uses_local_names(node))
        if error_context(node, names):
            wrapper = ast.Name(id="_blast_direct_block", ctx=ast.Load())
            args = [ast.Constant(ast.unparse(node)), ast.Constant(tuple(names)),
                    ast.Call(func=ast.Name(id="locals", ctx=ast.Load()), args=[], keywords=[])]
            outer = ast.withitem(context_expr=ast.Call(func=wrapper, args=args, keywords=[]))
            return ast.copy_location(ast.With(items=[outer], body=[node]), node)
        return self.generic_visit(node)

    def visit_Expr(self, node):
        call = node.value
        names = sorted(uses_local_names(call))
        if sql_assertion(call, names):
            wrapper = ast.Name(id="_blast_direct_assertion", ctx=ast.Load())
            return ast.copy_location(ast.Expr(ast.Call(
                func=wrapper, args=[ast.Constant(ast.unparse(call)), ast.Constant(tuple(names)),
                ast.Lambda(args=ast.arguments(posonlyargs=[], args=[], kwonlyargs=[], kw_defaults=[],
                                              defaults=[]), body=call)], keywords=[])), node)
        return self.generic_visit(node)


def test_code(loader, fullname):
    filename = loader.get_filename(fullname)
    if not filename.startswith(root + "tests/"):
        return normal_code(loader, fullname)
    before = ast.parse(Path(filename).read_bytes(), filename)
    revised = ast.fix_missing_locations(Instrument().visit(before))
    return compile(revised, filename, "exec", dont_inherit=True)
out = None


def entered(code, offset):
    path = code.co_filename
    if path.startswith(root + "sqlglot/"):
        record = objects.get(id(code))
        if record is None:
            key = path[len(root):], code.co_firstlineno, code.co_qualname
            number = code_ids.setdefault(key, len(code_ids))
            objects[id(code)] = code, number
        else:
            _, number = record
        if frames:
            frames[-1][0].add(number)
            if code.co_argcount and code.co_varnames[0] in ("self", "cls"):
                receiver = sys._getframe(1).f_locals.get(code.co_varnames[0])
                cls = receiver if issubclass(type(receiver), type) else type(receiver)
                for scope in receiver_scopes(cls):
                    frames[-1][0].add(code_ids.setdefault(scope, len(code_ids)))
        else:
            startup.add(number)
    return monitor.DISABLE


def emit(unit):
    keys, hazards, definition = unit
    payload = base64.b85encode(marshal.dumps(definition)).decode()
    out.write(json.dumps([active, payload, sorted(keys), sorted(hazards)], separators=(",", ":")) + "\n")


@contextmanager
def unit(definition):
    record = [set(), set(), definition]
    frames.append(record)
    monitor.restart_events()
    try:
        yield
    finally:
        frames.pop()
        emit(record)
        if frames and definition is not None and definition[0] == "subtest":
            frames[-1][1].update(record[1])
        monitor.restart_events()


def helper(name, function):
    @functools.wraps(function)
    def wrapped(self, *args, **kwargs):
        if active is None:
            return function(self, *args, **kwargs)
        payload = ["helper", name, args, kwargs] if literals(args) and literals(kwargs) else ["opaque", name]
        with unit(payload):
            return function(self, *args, **kwargs)
    return wrapped


def subtest(function):
    @contextmanager
    @functools.wraps(function)
    def wrapped(self, *args, **kwargs):
        if active is not None and not any(frame[2] and frame[2][0] == "helper" for frame in frames):
            with unit(["subtest", args, kwargs] if literals(args) and literals(kwargs) else ["opaque", "subtest"]):
                with function(self, *args, **kwargs):
                    yield
        else:
            with function(self, *args, **kwargs):
                yield
    return wrapped


def audit(event, args):
    if not frames:
        return
    if event == "open":
        path = args[0]
        if isinstance(path, (str, bytes)):
            name = os.fsdecode(path)
            if name.startswith(root + "sqlglot/") and name.endswith((".py", ".pyc")):
                return
        frames[-1][1].add("files")
    elif event.startswith(("os.putenv", "os.unsetenv")):
        if os.fsdecode(args[0]) != "PYTEST_CURRENT_TEST":
            frames[-1][1].add("environment")


def observed(name, function):
    @functools.wraps(function)
    def checked(*args, **kwargs):
        if frames:
            caller = sys._getframe(1).f_code.co_filename
            if caller.startswith((root + "sqlglot/", root + "tests/")):
                frames[-1][1].add(name)
        return function(*args, **kwargs)
    return checked


def replace(owner, name, replacement):
    originals.append((owner, name, getattr(owner, name)))
    setattr(owner, name, replacement)


def pytest_sessionstart(session):
    global out
    out = (Path(os.environ["ASSERTION_TRACE"]) / "units.jsonl").open("w")
    monitor.use_tool_id(tool, "assertion-boundaries")
    monitor.register_callback(tool, monitor.events.PY_START, entered)
    monitor.register_callback(tool, monitor.events.PY_RESUME, entered)
    monitor.set_events(tool, monitor.events.PY_START | monitor.events.PY_RESUME)
    sys.addaudithook(audit)
    SourceFileLoader.get_code = test_code
    builtins._blast_direct_assertion = direct
    builtins._blast_direct_block = direct_block


def pytest_collection_finish(session):
    mod = sys.modules.get("tests.dialects.test_dialect")
    transpile = sys.modules.get("tests.test_transpile")
    owners = {}
    if mod is not None:
        owners[mod.Validator] = tuple(name for name in HELPERS if name != "validate")
    if transpile is not None:
        owners[transpile.TestTranspile] = ("validate",)
    for item in session.items:
        cls = getattr(item, "cls", None)
        method = getattr(item, "originalname", None)
        if cls is None or not method or not admissible(cls, method, owners):
            opaque[item.nodeid].add("test-harness")
    for cls, names in owners.items():
        for name in names:
            replace(cls, name, helper(name, getattr(cls, name)))
    replace(unittest.TestCase, "subTest", subtest(unittest.TestCase.subTest))
    for name in ("assertLogs", "assertWarns", "assertWarnsRegex"):
        replace(unittest.TestCase, name, observed("warnings", getattr(unittest.TestCase, name)))
    for owner, name, reason in ((os, "getenv", "environment"),
                                (type(os.environ), "__getitem__", "environment"), (time, "time", "time"),
                                (time, "monotonic", "time"), (random, "random", "randomness"),
                                (random, "randint", "randomness"), (random, "choice", "randomness"),
                                (os, "urandom", "randomness"), (warnings, "warn", "warnings")):
        replace(owner, name, observed(reason, getattr(owner, name)))


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    global active
    active = item.nodeid
    try:
        with unit(None):
            yield
    finally:
        active = None


def pytest_sessionfinish(session, exitstatus):
    monitor.set_events(tool, 0)
    SourceFileLoader.get_code = normal_code
    del builtins._blast_direct_assertion
    del builtins._blast_direct_block
    for owner, name, previous in reversed(originals):
        setattr(owner, name, previous)
    out.close()
    output = Path(os.environ["ASSERTION_TRACE"])
    keys = [None] * len(code_ids)
    for key, i in code_ids.items():
        keys[i] = key
    metadata = {"code": keys, "startup": sorted(startup), "opaque": {key: sorted(value) for key, value in opaque.items()},
                "ids": [item.nodeid for item in session.items], "exit": int(exitstatus)}
    (output / "manifest.json").write_text(json.dumps(metadata, separators=(",", ":")))
