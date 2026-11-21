"""Profile clean class configuration access in parser, generator and tokenizer families.

PYTEST_DONT_REWRITE: install before upstream classes and their children are imported.
"""
import ast
import builtins
import sys
from functools import lru_cache
from importlib.machinery import SourceFileLoader

import trace_access as recorder
from trace_access import pytest_configure, pytest_runtest_setup, pytest_runtest_call, pytest_runtest_teardown, pytest_sessionfinish  # noqa: F401

FAMILIES = {"sqlglot.parser": "Parser", "sqlglot.generator": "Generator", "sqlglot.tokens": "_TokenizerBase"}
MODULES = ("sqlglot.parsers.", "sqlglot.generators.", "sqlglot.tokenizers.", "sqlglot.dialects.")
default_code = SourceFileLoader.get_code


@lru_cache(maxsize=None)
def definitions(cls, field):
    keys = []
    for base in type.__getattribute__(cls, "__mro__"):
        values = type.__getattribute__(base, "__dict__")
        module = values.get("__module__", "")
        if field in values and (module in FAMILIES or module.startswith(MODULES)):
            keys.append(f"component:{module.replace('.', '/')}.py:{base.__qualname__}:{field}")
    return keys


def observe(cls, field):
    owner = "*" if recorder.import_depth else recorder.owner
    if field.isupper():
        recorder.events[owner].update(definitions(cls, field))
    elif field == "__dict__":
        frame = sys._getframe(2)
        keys = recorder.raw_keys(frame.f_code.co_filename, frame.f_lineno)
        if keys is None:
            keys = type.__getattribute__(cls, "__dict__")
        for name in keys:
            if name.isupper():
                recorder.events[owner].update(definitions(cls, name))


class ComponentType(type):
    def __new__(meta, name, bases, namespace, **kwargs):
        cls = super().__new__(meta, name, bases, namespace, **kwargs)
        module = namespace.get("__module__", "")
        if (module in FAMILIES or module.startswith(MODULES)) and ("__getattribute__" not in namespace or module in FAMILIES):
            prefix = f"component:{module.replace('.', '/')}.py:{cls.__qualname__}:"
            for field, val in namespace.items():
                if field.isupper() and type(val) in (str, int, float, bool):
                    recorder.events["*"].add(prefix + "defined:" + field)
        return cls

    def __getattribute__(cls, name):
        if name.isupper() or name == "__dict__":
            observe(cls, name)
        return type.__getattribute__(cls, name)


def get_code(loader, fullname):
    if fullname not in FAMILIES:
        return default_code(loader, fullname)
    filename = loader.get_filename(fullname)
    with open(filename, "rb") as f:
        tree = ast.parse(f.read(), filename)
    base = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == FAMILIES[fullname])
    assert not base.keywords and not any(getattr(n, "name", "") == "__getattribute__" for n in base.body)
    base.keywords.append(ast.keyword(arg="metaclass", value=ast.Name(id="_component_type", ctx=ast.Load())))
    base.body.append(ast.parse("""
def __getattribute__(self, name):
    if name.isupper():
        _component_access(type(self), name)
    return object.__getattribute__(self, name)
""").body[0])
    return compile(ast.fix_missing_locations(tree), filename, "exec", dont_inherit=True)


def install():
    recorder.install()
    builtins._component_type = ComponentType
    builtins._component_access = observe
    SourceFileLoader.get_code = get_code
