"""Track non-import consumers of package attributes that use PEP 562."""
import ast
import dis
import sys
import types
from functools import wraps
from importlib.machinery import SourceFileLoader
from pathlib import Path

import trace_access as record
from trace_access import pytest_configure, pytest_runtest_setup, pytest_runtest_call, pytest_runtest_teardown, pytest_sessionfinish  # noqa: F401



def watch(module, package, exports):
    class TrackedPackage(types.ModuleType):
        def __getattribute__(self, name):
            cached = name in types.ModuleType.__getattribute__(self, "__dict__")
            result = super().__getattribute__(name)
            if (name not in vars(types.ModuleType) and name not in vars(object)
                    and (name in exports or not cached)):
                frame = sys._getframe(1)
                opcode = dis.opname[frame.f_code.co_code[frame.f_lasti]]
                existence = (frame.f_code.co_name == "_handle_fromlist"
                             and frame.f_globals.get("__name__") == "importlib._bootstrap")
                if not existence and not (opcode == "IMPORT_FROM" and cached):
                    owner = "*" if record.import_depth else record.owner
                    label = f"{frame.f_globals.get('__name__')}:{frame.f_code.co_name}:{frame.f_lineno}:{name}"
                    record.events[owner].add("lazy:" + package + ":read")
                    record.events[owner].add("origin:" + package + ":" + label)
            return result
    original = vars(module)["__getattr__"]

    @wraps(original)
    def observe_hook(name):
        value = original(name)
        caller = sys._getframe(1)
        if caller.f_code is not TrackedPackage.__getattribute__.__code__:
            owner = "*" if record.import_depth else record.owner
            record.events[owner].add("lazy:" + package + ":direct-hook")
        return value

    module.__getattr__ = observe_hook
    module.__class__ = TrackedPackage


def exec_module(loader, module):
    record.exec_module(loader, module)
    path = getattr(module, "__file__", None)
    if (not path or not module.__name__.startswith("sqlglot.")
            or not path.endswith("/__init__.py") or "__getattr__" not in vars(module)):
        return
    tree = ast.parse(Path(path).read_bytes())
    exports = set(vars(module).get("_LAZY_ATTRS", ()))
    exports.update(p.stem for p in Path(path).parent.glob("*.py") if p.stem != "__init__")
    if exports and any(isinstance(n, ast.FunctionDef) and n.name == "__getattr__" for n in tree.body):
        package = module.__name__
        record.events["*"].add("lazy:" + package + ":defined")
        watch(module, package, exports)


def install():
    record.install()
    SourceFileLoader.exec_module = exec_module
