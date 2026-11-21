"""Observe dialect field resolution, module discovery and directory reads on the clean base.

PYTEST_DONT_REWRITE: install hooks before the package is imported.
"""

import ast
import builtins
import json
import os
import sys
import zlib
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from functools import lru_cache
from importlib.machinery import SourceFileLoader
from multiprocessing.util import Finalize
from pathlib import Path

import pytest

root = str(Path.cwd()) + "/"
owner = "*"
events = defaultdict(set)
items = []
import_depth = 0
normal_exec = SourceFileLoader.exec_module
normal_stat, normal_lstat = os.stat, os.lstat
normal_code = SourceFileLoader.get_code
normal_submit = ProcessPoolExecutor.submit
installed = False


@lru_cache(maxsize=None)
def definitions(cls, name):
    # Keep all declarations along the MRO, including shadowed parent fields.
    out = []
    for parent in type.__getattribute__(cls, "__mro__"):
        ns = type.__getattribute__(parent, "__dict__")
        module = ns.get("__module__", "")
        if name in ns and module.startswith("sqlglot.dialects."):
            out.append(
                "field:"
                + module.replace(".", "/")
                + ".py:"
                + parent.__qualname__
                + ":"
                + name
            )
    return tuple(out)


@lru_cache(maxsize=None)
def raw_keys(filename, line):
    if not filename.startswith(root):
        return None
    tree = ast.parse(Path(filename).read_bytes())
    parents = {
        child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)
    }
    found = set()
    for expr in parents:
        if (
            not isinstance(expr, ast.Attribute)
            or expr.attr != "__dict__"
            or expr.lineno != line
        ):
            continue
        parent = parents[expr]
        if isinstance(parent, ast.Subscript):
            key = parent.slice
        elif isinstance(parent, ast.Attribute) and parent.attr == "get":
            call = parents[parent]
            if not isinstance(call, ast.Call) or not call.args:
                return None
            key = call.args[0]
        else:
            return None
        if not isinstance(key, ast.Constant) or not isinstance(key.value, str):
            return None
        found.add(key.value)
    return found or None


def watch(cls, name, meta):
    context = "*" if import_depth else owner
    if name.isupper():
        events[context].update(definitions(cls, name))
    elif meta and name == "__dict__":
        caller = sys._getframe(2)
        keys = raw_keys(caller.f_code.co_filename, caller.f_lineno)
        if keys is None:
            keys = type.__getattribute__(cls, "__dict__").keys()
        for key in keys:
            if key.isupper():
                events[context].update(definitions(cls, key))


def exec_module(loader, module):
    global import_depth
    import_depth += 1
    try:
        return normal_exec(loader, module)
    finally:
        import_depth -= 1


def get_code(loader, fullname):
    if fullname != "sqlglot.dialects.dialect":
        return normal_code(loader, fullname)
    filename = loader.get_filename(fullname)
    tree = ast.parse(Path(filename).read_bytes(), filename)
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name in ("Dialect", "_Dialect"):
            assert not any(
                getattr(n, "name", "") == "__getattribute__" for n in node.body
            )
            meta = node.name == "_Dialect"
            receiver = "self" if meta else "type(self)"
            parent = "type" if meta else "object"
            method = ast.parse(
                f"def __getattribute__(self, name):\n"
                f"    _observe_dialect_member({receiver}, name, {meta})\n"
                f"    return {parent}.__getattribute__(self, name)"
            ).body[0]
            node.body.append(method)
    return compile(ast.fix_missing_locations(tree), filename, "exec", dont_inherit=True)


class ObserveLookup:
    def find_spec(self, fullname, path, target=None):
        if fullname.startswith("sqlglot."):
            events[owner].add("module:" + fullname)
        return None


def probe(path):
    if not isinstance(path, (str, bytes, os.PathLike)):
        return
    path = os.path.abspath(os.fsdecode(path))
    if os.path.dirname(path) == root + "sqlglot" and path.endswith(".py"):
        events[owner].add("path:" + os.path.basename(path))


def stat_hook(fn):
    def stat(path, *args, **kwargs):
        try:
            return fn(path, *args, **kwargs)
        except OSError:
            probe(path)
            raise

    return stat


def audit(event, args):
    if event == "open":
        probe(args[0])
        return
    if event not in ("os.listdir", "os.scandir"):
        return
    path = args[0]
    if isinstance(path, int):
        try:
            path = os.readlink(f"/proc/self/fd/{path}")
        except OSError:
            events[owner].add("dir:?")
            return
    path = os.path.abspath(os.fsdecode(path or "."))
    if path == root + "sqlglot":
        # FileFinder refreshes a name cache; actual name queries have their own hook.
        frame = sys._getframe(1)
        module = frame.f_globals.get("__name__")
        if (
            module == "importlib._bootstrap_external"
            and frame.f_code.co_name == "_fill_cache"
        ):
            return
        parent = frame.f_back
        if (
            module == "_pytest.pathlib"
            and frame.f_code.co_name == "scandir"
            and parent
            and parent.f_globals.get("__name__") == "_pytest.python"
            and parent.f_code.co_name == "collect"
        ):
            events[owner].add("pytest:collect")
        else:
            events[owner].add("dir:sqlglot")


def save():
    dst = Path(os.environ["TRACE_OUTPUT"]) / f"access-{os.getpid()}.z"
    dst.write_bytes(
        zlib.compress(
            json.dumps([items, {k: sorted(v) for k, v in events.items()}]).encode()
        )
    )


def install():
    global installed
    if installed:
        return
    installed = True
    sys.meta_path.insert(0, ObserveLookup())
    sys.addaudithook(audit)
    builtins._observe_dialect_member = watch
    SourceFileLoader.get_code = get_code
    SourceFileLoader.exec_module = exec_module
    os.stat, os.lstat = stat_hook(normal_stat), stat_hook(normal_lstat)
    Finalize(None, save, exitpriority=1)


def work(context, fn, args, kwargs):
    global owner
    owner = context
    return fn(*args, **kwargs)


def submit(pool, fn, /, *args, **kwargs):
    return normal_submit(pool, work, owner, fn, args, kwargs)


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
