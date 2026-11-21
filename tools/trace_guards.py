"""Recognize equality methods that short-circuit before comparing unrelated types.

PYTEST_DONT_REWRITE: uses the unchanged operand recorder in parent and worker processes.
"""
import ast
import inspect
import textwrap
from functools import lru_cache

import trace_operands as recorder
from trace_operands import pytest_configure, pytest_runtest_setup, pytest_runtest_call, pytest_runtest_teardown, pytest_sessionfinish  # noqa: F401

original = recorder.observe


@lru_cache(maxsize=None)
def guard_files(cls):
    eq, ne = cls.__eq__, cls.__ne__
    if (not inspect.isfunction(eq) or not inspect.isfunction(ne)
            or cls.__getattribute__ is not object.__getattribute__
            or eq.__globals__.get("type", type) is not type):
        return ()
    try:
        eq_tree, ne_tree = [ast.parse(textwrap.dedent(inspect.getsource(f))).body[0] for f in (eq, ne)]
    except (OSError, TypeError, SyntaxError):
        return ()
    if any(len(t.body) != 1 or not isinstance(t.body[0], ast.Return) or len(t.args.args) != 2
           or t.decorator_list for t in (eq_tree, ne_tree)):
        return ()
    a, b = [arg.arg for arg in eq_tree.args.args]
    x, y = [arg.arg for arg in ne_tree.args.args]
    value = eq_tree.body[0].value
    if not (isinstance(value, ast.BoolOp) and isinstance(value.op, ast.Or) and len(value.values) == 2):
        return ()
    identity, conjunction = value.values
    if not (isinstance(conjunction, ast.BoolOp) and isinstance(conjunction.op, ast.And)):
        return ()
    def matches(node, source):
        return ast.dump(node) == ast.dump(ast.parse(source, mode="eval").body)
    if (matches(identity, f"{a} is {b}") and matches(conjunction.values[0], f"type({a}) is type({b})")
            and matches(ne_tree.body[0].value, f"not {x}.__eq__({y})")):
        return (eq.__code__.co_filename, ne.__code__.co_filename)
    return ()


def observe(key, value):
    cls = type(value)
    if recorder.describe(cls) == "unresolved":
        files = guard_files(cls)
        if (files and all(not f.endswith("/" + key.split(":", 1)[0]) for f in files)
                and "__eq__" not in getattr(value, "__dict__", {})):
            recorder.samples[recorder.owner].add((key, "none-safe"))
            return value
    return original(key, value)


def install():
    recorder.observe = observe
    recorder.install()
