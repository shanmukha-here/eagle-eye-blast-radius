"""Trace reads of the built-in dialect registry and its string enumeration."""
import ast
import builtins
import enum
import os
import pytest
import sys
from types import MappingProxyType
from importlib.machinery import SourceFileLoader
from pathlib import Path

import trace_access as rec
from trace_access import pytest_configure, pytest_sessionfinish  # noqa: F401

original_code = SourceFileLoader.get_code
module = "sqlglot.dialects.dialect"
registration = None
key_reads = set()


def note(key):
    owner = "*" if rec.import_depth else rec.owner
    rec.events[owner].add("registry:" + key)
    if owner == "*":
        frame = sys._getframe(2)
        rec.events[owner].add(f"regorigin:{key}:{frame.f_globals.get('__name__')}:{frame.f_code.co_name}:{frame.f_lineno}")


def register_read(frame):
    return (frame.f_globals.get("__name__") == module
            and frame.f_code.co_qualname == "_Dialect.__new__"
            and frame.f_lineno in (registration, registration + 1))


class Meta(enum.EnumType):
    def __getattribute__(cls, attr):
        if vars(sys.modules[module]).get("Dialects") is not cls:
            return super().__getattribute__(attr)
        frame = sys._getframe(1)
        if attr == "__members__" and register_read(frame):
            return MappingProxyType(type.__getattribute__(cls, "_member_map_"))
        elif attr.isupper():
            note("member:" + attr)
        elif attr in {"__members__", "_member_map_", "_member_names_", "_value2member_map_", "__dict__"}:
            # Enum's own creation precedes publication of the class.
            if frame.f_globals.get("__name__") != "enum" or frame.f_code.co_name not in {"__new__", "__set_name__"}:
                note("all")
        return super().__getattribute__(attr)

    def __call__(cls, value, *args, **kwargs):
        note("key:" + value if type(value) is str else "all")
        return super().__call__(value, *args, **kwargs)

    def __iter__(cls):
        note("all")
        return super().__iter__()


def observe(frame):
    if register_read(frame):
        return
    if frame.f_globals.get("__name__") == module and frame.f_lineno in key_reads:
        key = frame.f_locals.get("key")
        if type(key) is str:
            note("key:" + key)
            return
    if rec.import_depth or rec.owner == "*":
        rec.events["*"].add(f"regorigin:external:{frame.f_globals.get('__name__')}:{frame.f_code.co_name}:{frame.f_lineno}")
    if (frame.f_globals.get("__name__") == "__main__" and rec.owner == "*"
            and not rec.import_depth and os.environ.get("BLAST_REGISTRY_TEST")):
        rec.events[os.environ["BLAST_REGISTRY_TEST"]].add("registry:all")
        return
    note("all")


def access(value, key):
    note("key:" + key if type(key) is str else "all")
    return value


def get_code(loader, fullname):
    global registration
    if fullname != module:
        return original_code(loader, fullname)
    filename = loader.get_filename(fullname)
    source = Path(filename).read_text()
    tree = ast.parse(source, filename)
    for cls in tree.body:
        if not isinstance(cls, ast.ClassDef):
            continue
        if cls.name == "Dialects":
            assert not cls.keywords
            cls.keywords.append(ast.keyword(arg="metaclass", value=ast.Name(id="_registry_enum_meta", ctx=ast.Load())))
        elif cls.name == "_Dialect":
            assert not any(getattr(n,"name",None) == "__getattribute__" for n in cls.body)
            for fn in cls.body:
                if not isinstance(fn,ast.FunctionDef):
                    continue
                for node in ast.walk(fn):
                    segment = ast.get_source_segment(source, node) if isinstance(node,(ast.Subscript,ast.Compare,ast.Call,ast.Assign)) and hasattr(node,"lineno") else None
                    if segment in ("cls._classes[key]", "key not in cls._classes", "cls._classes.get(key, default)"):
                        key_reads.add(node.lineno)
                    if segment == "enum = Dialects.__members__.get(clsname.upper())":
                        registration = node.lineno
            class RegistryReads(ast.NodeTransformer):
                def visit_Attribute(self, node):
                    if node.attr != "_classes" or node.lineno in (registration, registration + 1):
                        return self.generic_visit(node)
                    key = ast.Name(id="key", ctx=ast.Load()) if node.lineno in key_reads else ast.Constant(None)
                    return ast.copy_location(ast.Call(ast.Name(id="_registry_read", ctx=ast.Load()),
                                                      [node, key], []), node)
            RegistryReads().visit(cls)
            cls.body.append(ast.parse("""
def __getattribute__(cls, name):
    if name == "_classes" and _registry_frame(1).f_globals.get("__name__") != "sqlglot.dialects.dialect":
        _registry_observe(_registry_frame(1))
    return type.__getattribute__(cls, name)
""").body[0])
    assert registration is not None
    return compile(ast.fix_missing_locations(tree), filename, "exec", dont_inherit=True)


def install():
    rec.install()
    builtins._registry_enum_meta = Meta
    builtins._registry_read = access
    builtins._registry_observe = observe
    builtins._registry_frame = sys._getframe
    SourceFileLoader.get_code = get_code


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item):
    rec.pytest_runtest_setup(item)
    os.environ["BLAST_REGISTRY_TEST"] = rec.owner


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_call(item):
    rec.pytest_runtest_call(item)
    os.environ["BLAST_REGISTRY_TEST"] = rec.owner


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_teardown(item, nextitem):
    rec.pytest_runtest_teardown(item, nextitem)
    os.environ["BLAST_REGISTRY_TEST"] = rec.owner
