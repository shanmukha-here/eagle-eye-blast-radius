"""Trace observations of the values of the clean package's name-based enums.

PYTEST_DONT_REWRITE: installation precedes imports in the main and spawned interpreters.
"""
import ast
import builtins
import enum
import sys
from functools import lru_cache
from importlib.machinery import SourceFileLoader
from pathlib import Path

import trace_access as recorder
from trace_access import pytest_configure, pytest_runtest_setup, pytest_runtest_call, pytest_runtest_teardown, pytest_sessionfinish  # noqa: F401

original_code = SourceFileLoader.get_code
creating = []


@lru_cache(maxsize=None)
def identifier(cls):
    return ("enum:" + type.__getattribute__(cls, "__module__").replace(".", "/")
            + ".py:" + type.__getattribute__(cls, "__qualname__") + ":")


def note(cls, kind):
    if type.__getattribute__(cls, "__name__") not in creating:
        owner = "*" if recorder.import_depth else recorder.owner
        recorder.events[owner].add(identifier(cls) + kind)


def member_read(member, name):
    if name in ("_value_", "__dict__"):
        data = object.__getattribute__(member, "__dict__")
        if "_name_" in data:
            note(type(member), "value:" + data["_name_"])


class ObservedEnumType(enum.EnumType):
    def __new__(meta, name, bases, namespace, **kwargs):
        creating.append(name)
        try:
            cls = super().__new__(meta, name, bases, namespace, **kwargs)
        finally:
            creating.pop()
        # Registers complete, ordinary enumerations even when no value is read.
        recorder.events["*"].add(identifier(cls) + "defined")
        return cls

    def __call__(cls, value, *args, **kwargs):
        if type(value) is str:
            note(cls, "lookup:" + value)
        elif type(value) is not cls:
            note(cls, "unknown")
        return super().__call__(value, *args, **kwargs)

    def __contains__(cls, value):
        if type(value) is str:
            note(cls, "lookup:" + value)
        elif type(value) is not cls:
            note(cls, "unknown")
        return super().__contains__(value)

    def __getattribute__(cls, name):
        if name in ("_value2member_map_", "_unhashable_values_", "_unhashable_values_map_",
                    "_hashable_values_", "__dict__"):
            caller = sys._getframe(1)
            if caller.f_code in (enum.Enum.__new__.__code__, enum.EnumType.__contains__.__code__):
                value = caller.f_locals.get("value")
                if type(value) is str:
                    note(cls, "lookup:" + value)
                elif type(value) is not cls:
                    note(cls, "unknown")
            else:
                note(cls, "unknown")
        return super().__getattribute__(name)


def get_code(loader, fullname):
    if fullname != "sqlglot.helper":
        return original_code(loader, fullname)
    path = loader.get_filename(fullname)
    tree = ast.parse(Path(path).read_bytes(), path)
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "AutoName")
    assert not cls.keywords and not any(getattr(n, "name", "") == "__getattribute__" for n in cls.body)
    cls.keywords.append(ast.keyword(arg="metaclass", value=ast.Name(id="_observed_enum_type", ctx=ast.Load())))
    cls.body.append(ast.parse("""
def __getattribute__(self, name):
    if name == "_value_" or name == "__dict__":
        _observe_enum_value(self, name)
    return object.__getattribute__(self, name)
""").body[0])
    return compile(ast.fix_missing_locations(tree), path, "exec", dont_inherit=True)


def install():
    recorder.install()
    builtins._observed_enum_type = ObservedEnumType
    builtins._observe_enum_value = member_read
    SourceFileLoader.get_code = get_code
