"""Observe lists initialized from SQL fixture pairs by upstream unittest classes."""
import ast
import inspect
import json
import os
import textwrap
from pathlib import Path

import pytest

records = {}


class ObservedList(list):
    def __init__(self, values):
        super().__init__(values)
        self.read = False

    def __iter__(self):
        self.read = True
        return super().__iter__()

    def __getitem__(self, key):
        self.read = True
        return super().__getitem__(key)

    def __len__(self):
        self.read = True
        return super().__len__()


def fields(cls):
    tree = ast.parse(textwrap.dedent(inspect.getsource(cls.setUpClass)))
    for stmt in ast.walk(tree):
        if not isinstance(stmt, ast.Assign) or len(stmt.targets) != 1:
            continue
        target = stmt.targets[0]
        value = stmt.value
        if not (isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name)
                and isinstance(value, ast.Call) and ast.unparse(value.func) == 'list'
                and len(value.args) == 1 and isinstance(value.args[0], ast.Call)):
            continue
        load = value.args[0]
        if (ast.unparse(load.func) == 'load_sql_fixture_pairs' and len(load.args) == 1
                and isinstance(load.args[0], ast.Constant) and type(load.args[0].value) is str):
            yield target.attr, 'tests/fixtures/' + load.args[0].value


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    cls = item.cls
    wrapped = []
    if cls and hasattr(cls, 'setUpClass'):
        for name, path in fields(cls):
            original = getattr(cls, name)
            if type(original) is list:
                wrapper = ObservedList(original)
                setattr(cls, name, wrapper)
                wrapped.append((name, path, original, wrapper))
    try:
        yield
    finally:
        for name, path, original, wrapper in wrapped:
            # Direct attribute references in the test or a reachable helper may
            # also consume the list through a C routine bypassing Python hooks.
            records.setdefault(path, {})[item.nodeid] = wrapper.read
            setattr(cls, name, original)


def pytest_sessionfinish(session, exitstatus):
    if exitstatus == 0:
        Path(os.environ['FIXTURE_TRACE']).write_text(json.dumps(records, sort_keys=True))
