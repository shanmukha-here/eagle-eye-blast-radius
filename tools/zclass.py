"""Class and name maps of the pinned source, for edits that add or delete a method.

Classes: every class defined at module or class level in sqlglot, after importing all of
sqlglot, with its method resolution order and, for each name in its own namespace, the code
key of the function it holds (plain, class or static method, or property getter) or None.
Names: for every Python file in sqlglot/ and tests/, the identifiers it uses, the strings it
contains and the names it compares, as zpatch.names computes them.

usage: zclass.py ROOT OUT
"""

import lzma
import marshal
import os
import sys
import types

ROOT, OUT = map(os.path.abspath, sys.argv[1:3])
os.chdir(ROOT)
sys.path.insert(0, ROOT)
sys.path.insert(1, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import zpatch  # noqa: E402
import zrun  # noqa: E402

zrun.preload_all(ROOT)
PREFIX = os.path.join(ROOT, "sqlglot") + os.sep


def code_key(code):
    if not code.co_filename.startswith(PREFIX):
        return None
    return (code.co_filename[len(ROOT) + 1:], code.co_qualname, code.co_firstlineno)


def entry(value):
    """("function"|"classmethod"|"staticmethod"|"property", key) or None for anything else."""
    kind = "function"
    if isinstance(value, classmethod):
        kind, value = "classmethod", value.__func__
    elif isinstance(value, staticmethod):
        kind, value = "staticmethod", value.__func__
    elif isinstance(value, property):
        if value.fset is not None or value.fdel is not None:
            return None
        kind, value = "property", value.fget
    if isinstance(value, types.FunctionType):
        key = code_key(value.__code__)
        if key is not None:
            return (kind, key)
    return None


classes = {}
seen = set()
for module in list(sys.modules.values()):
    path = getattr(module, "__file__", None) or ""
    if not path.startswith(PREFIX):
        continue
    stack = [v for v in vars(module).values() if isinstance(v, type)]
    while stack:
        cls = stack.pop()
        if id(cls) in seen:
            continue
        seen.add(id(cls))
        file = getattr(sys.modules.get(cls.__module__), "__file__", None) or ""
        if not file.startswith(PREFIX) or "<locals>" in cls.__qualname__:
            continue
        stack.extend(v for v in vars(cls).values() if isinstance(v, type))
        classes[cls] = (file[len(ROOT) + 1:], cls.__qualname__)

index = {}
order = []
for cls, key in classes.items():
    if key in index:
        index[key] = -1  # defined twice: unusable
        continue
    index[key] = len(order)
    order.append(cls)
number = {cls: i for i, cls in enumerate(order)}
external = {}
records = []
for cls in order:
    mro = []
    for base in cls.__mro__:
        if base in number:
            mro.append(number[base])
        else:
            name = base.__module__ + "." + base.__qualname__
            if name not in external:
                external[name] = sorted(vars(base))
            mro.append(name)
    names = {}
    for name, value in vars(cls).items():
        names[name] = entry(value)
    records.append((classes[cls], mro, names))

files = {}
for top in ("sqlglot", "tests"):
    for directory, _, filenames in os.walk(os.path.join(ROOT, top)):
        for filename in filenames:
            if filename.endswith(".py"):
                path = os.path.join(directory, filename)
                with open(path, encoding="utf-8", newline="") as f:
                    text = f.read()
                try:
                    files[path[len(ROOT) + 1:]] = zpatch.names(text)
                except SyntaxError:
                    files[path[len(ROOT) + 1:]] = None

data = {"classes": records, "unique": {k: v for k, v in index.items()}, "external": external,
        "files": files}
with open(OUT, "wb") as f:
    f.write(lzma.compress(marshal.dumps(data), preset=9 | lzma.PRESET_EXTREME))
print(len(records), "classes", len(external), "external", len(files), "files", file=sys.stderr)
