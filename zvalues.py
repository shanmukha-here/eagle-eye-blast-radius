"""Digests of every module global and class attribute in sqlglot after importing all of it.

Two imports whose digests agree for a name hold equal values there, up to the identity of
classes, modules and code (code is compared by its contents, not its line numbers). Values of
a kind this module cannot read are reported under "opaque", with the names that reach them.
"""

import enum
import functools
import hashlib
import os
import re
import sys
import types

_SCALARS = (type(None), bool, int, float, complex, str, bytes)
_KEYS, _VALUES, _ITEMS = type({}.keys()), type({}.values()), type({}.items())
_TUPLEGETTER = type(__import__("collections").namedtuple("_N", "a").a)


class Digest:
    def __init__(self, prefix, ordered=True):
        self.prefix = prefix
        self.ordered = ordered
        self.memo = {}
        self.stack = {}
        self.opaque = set()
        self.keep = []

    def code(self, c):
        parts = [b"C", c.co_name.encode(), c.co_qualname.encode(), c.co_code,
                 repr((c.co_names, c.co_varnames, c.co_freevars, c.co_cellvars, c.co_flags,
                       c.co_argcount, c.co_posonlyargcount, c.co_kwonlyargcount)).encode()]
        for k in c.co_consts:
            parts.append(self.code(k) if isinstance(k, types.CodeType) else self(k))
        return hashlib.blake2b(b"\0".join(parts), digest_size=16).digest()

    def __call__(self, v):
        t = type(v)
        if t in _SCALARS:
            return hashlib.blake2b(t.__name__.encode() + b":" + repr(v).encode(), digest_size=16).digest()
        i = id(v)
        d = self.memo.get(i)
        if d is not None:
            return d
        if i in self.stack:
            return b"cycle:%d" % self.stack[i]
        self.stack[i] = len(self.stack)
        self.keep.append(v)
        try:
            parts = self.parts(v, t)
        finally:
            del self.stack[i]
        d = hashlib.blake2b(b"\0".join(parts), digest_size=16).digest()
        self.memo[i] = d
        return d

    def name(self, v):
        return ("%s.%s" % (getattr(v, "__module__", "?"), getattr(v, "__qualname__", "?"))).encode()

    def parts(self, v, t):
        if t in (tuple, list):
            items = [self(x) for x in v]
            # Unordered digests also ignore sequence order: such keys were built from sets.
            return [t.__name__.encode()] + (items if self.ordered else sorted(items))
        if t in (set, frozenset):
            return [t.__name__.encode()] + sorted(self(x) for x in v)
        if t is dict or t is types.MappingProxyType:
            pairs = [self(k) + self(x) for k, x in v.items()]
            return [b"dict"] + (pairs if self.ordered else sorted(pairs))
        if t in (_KEYS, _VALUES, _ITEMS):
            return [t.__name__.encode(), self(list(v))]
        if t.__module__ in ("typing", "_thread", "_abc", "datetime") or t is _TUPLEGETTER:
            # Type hints, locks, ABC caches, dates and named tuple fields: by their text.
            text = repr(v) if t.__module__ != "_abc" else ""
            if t is _TUPLEGETTER:
                text = repr(v.__reduce__()[1])
            elif t.__module__ == "_thread":
                text = t.__name__
            return [b"R", self.name(t), text.encode()]
        if isinstance(v, type):
            return [b"T", self.name(v)]
        if t is types.ModuleType:
            return [b"M", v.__name__.encode()]
        if isinstance(v, enum.Enum):
            return [b"E", self.name(t), v.name.encode()]
        if t is types.FunctionType:
            cells = []
            for c in v.__closure__ or ():
                try:
                    cells.append(self(c.cell_contents))
                except ValueError:
                    cells.append(b"empty")
            return [b"F", self.code(v.__code__), self(v.__defaults__), self(v.__kwdefaults__),
                    self(v.__module__)] + cells + [self(dict(v.__dict__))]
        if t is types.CodeType:
            return [self.code(v)]
        if t in (types.BuiltinFunctionType, types.MethodWrapperType, types.WrapperDescriptorType,
                 types.MethodDescriptorType, types.ClassMethodDescriptorType, types.GetSetDescriptorType,
                 types.MemberDescriptorType):
            owner = getattr(v, "__self__", None)
            parts = [b"B", self.name(v), getattr(v, "__name__", "").encode()]
            if owner is not None and not isinstance(owner, types.ModuleType):
                parts.append(self(owner))
            return parts
        if t is types.MethodType:
            return [b"BM", self(v.__self__), self(v.__func__)]
        if t in (classmethod, staticmethod):
            return [t.__name__.encode(), self(v.__func__)]
        if t is property:
            return [b"P", self(v.fget), self(v.fset), self(v.fdel)]
        if t is functools.partial:
            return [b"partial", self(v.func), self(v.args), self(v.keywords)]
        if t is re.Pattern:
            return [b"re", self(v.pattern), self(v.flags)]
        if isinstance(v, (functools.cached_property,)):
            return [b"cp", self(v.func)]
        state = getattr(v, "__dict__", None)
        slots = []
        for k in t.__mro__:
            s = getattr(k, "__slots__", ())
            slots += [s] if isinstance(s, str) else [x for x in s if x not in ("__dict__", "__weakref__")]
        parts = [b"O", self.name(t)]
        known = False
        if isinstance(v, dict):
            parts.append(self(dict(v))); known = True
        elif isinstance(v, (list, tuple, set, frozenset)):
            parts.append(self(list(v) if not isinstance(v, (set, frozenset)) else frozenset(v))); known = True
        if t.__module__ == "builtins" or not (known or state is not None or slots):
            self.opaque.add(self.name(t).decode())
            return [b"?", self.name(t), b"%d" % id(v)]
        if state is not None:
            parts.append(self(dict(state)))
        for s in slots:
            parts.append(self(getattr(v, s, "<unset>")))
        return parts


_SKIP = {"__module__", "__qualname__", "__doc__", "__dict__", "__weakref__", "__firstlineno__",
         "__static_attributes__", "__builtins__", "__cached__", "__loader__", "__spec__", "__file__",
         "__path__", "__package__", "__name__"}


def items(root):
    """(key, value) for every module global and class attribute of the imported sqlglot, with
    keys ("m", file, name) and ("c", file, class qualname, name)."""
    prefix = os.path.join(root, "sqlglot") + os.sep
    seen = set()
    for module in list(sys.modules.values()):
        path = getattr(module, "__file__", None) or ""
        if not path.startswith(prefix):
            continue
        rel = path[len(root) + 1:]
        stack = []
        for name, v in list(vars(module).items()):
            if name in _SKIP:
                continue
            yield ("m", rel, name), v
            if isinstance(v, type):
                stack.append(v)
        while stack:
            cls = stack.pop()
            if id(cls) in seen or getattr(sys.modules.get(cls.__module__), "__file__", None) != path \
                    or "<locals>" in cls.__qualname__:
                continue
            seen.add(id(cls))
            for name, v in list(vars(cls).items()):
                if name in _SKIP:
                    continue
                yield ("c", rel, cls.__qualname__, name), v
                if isinstance(v, type):
                    stack.append(v)


def code_keys(root, v):
    """Code keys of the functions a function-like value runs, or None for other values."""
    if isinstance(v, (classmethod, staticmethod)):
        v = v.__func__
    if isinstance(v, property):
        funcs = [f for f in (v.fget, v.fset, v.fdel) if f is not None]
    elif isinstance(v, functools.cached_property):
        funcs = [v.func]
    elif isinstance(v, types.FunctionType):
        funcs = [v]
    else:
        return None
    out = []
    for f in funcs:
        f = getattr(f, "__wrapped__", f)
        if not isinstance(f, types.FunctionType):
            return None
        c = f.__code__
        if not c.co_filename.startswith(root + os.sep):
            return None
        out.append((c.co_filename[len(root) + 1:], c.co_qualname, c.co_firstlineno))
    return out


def snapshot(root, ordered=True, only=None):
    """({key: digest}, opaque type names); with ``only``, just those keys."""
    dig = Digest(os.path.join(root, "sqlglot") + os.sep, ordered)
    out = {}
    for key, v in items(root):
        if only is None or key in only:
            out[key] = dig(v)
    return out, sorted(dig.opaque)


class _TextLoader(__import__("importlib.abc").abc.SourceLoader):
    def __init__(self, name, path, text):
        self.name, self.path, self.text = name, path, text

    def get_filename(self, fullname):
        return self.path

    def get_data(self, path):
        if path == self.path:
            return self.text.encode("utf-8")
        with open(path, "rb") as f:
            return f.read()


class Overlay:
    """Meta path finder that serves the given sqlglot files' new text in place of the pin's."""

    def __init__(self, root, files):
        self.by_name = {}
        for rel, text in files.items():
            name = rel[:-3].replace("/", ".")
            package = name.endswith(".__init__")
            if package:
                name = name[: -len(".__init__")]
            self.by_name[name] = (os.path.join(root, rel), text, package)

    def find_spec(self, name, path=None, target=None):
        hit = self.by_name.get(name)
        if hit is None:
            return None
        import importlib.util
        file, text, package = hit
        return importlib.util.spec_from_file_location(
            name, file, loader=_TextLoader(name, file, text),
            submodule_search_locations=[os.path.dirname(file)] if package else None)


def diff(root, files, pin):
    """Import sqlglot with ``files`` ({path: new text}) replacing the pin's sources and compare
    with ``pin`` (the zval map). Returns (changed, added, removed) key lists."""
    sys.meta_path.insert(0, Overlay(root, files))
    import zrun
    failed = zrun.preload_all(root)
    if failed:
        raise ImportError(failed)
    new, _ = snapshot(root)
    loose = pin["unordered"]
    if loose:
        again, _ = snapshot(root, False, set(loose))
    else:
        again = {}
    old = pin["digests"]
    changed = []
    for key, d in new.items():
        if key in loose:
            if again.get(key) != loose[key]:
                changed.append(key)
        elif key in old and old[key] != d:
            changed.append(key)
    added = [k for k in new if k not in old]
    removed = [k for k in old if k not in new]
    return changed, added, removed


def main():
    """Child: read marshal (root, map path, {path: text}) on stdin, write marshal result."""
    import lzma
    import marshal
    root, map_path, files = marshal.loads(sys.stdin.buffer.read())
    n = int(os.environ.get("ZVAL_PERTURB", "0"))  # map-build checks only
    keep = [bytearray(16 * (1 + i % 5)) for i in range(n)]  # noqa: F841
    sys.path.insert(0, root)
    with open(map_path, "rb") as f:
        data = marshal.loads(lzma.decompress(f.read()))
    pin = {"digests": dict(zip(map(tuple, data["keys"]), data["digests"])),
           "unordered": dict(zip(map(tuple, data["loose_keys"]), data["loose"]))}
    try:
        result = diff(root, files, pin)
    except BaseException as e:  # noqa: BLE001
        result = ("error", type(e).__name__)
    sys.stdout.buffer.write(marshal.dumps(result))
    sys.stdout.flush()
    os._exit(0)


if __name__ == "__main__":
    main()
