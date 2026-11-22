"""Run candidate tests on patched code in forks of one interpreter that imported the suite.

The server imports the numbered test modules once, as pytest collection would. For each job
it forks a child that swaps the edited functions' code objects in place (closures created
earlier are found by their code object), updates docstrings and literal defaults, serves the
patched text to modules imported later, then runs the requested tests with unittest.
"""

import gc
import importlib.abc
import importlib.util
import marshal
import os
import select
import signal
import struct
import subprocess
import sys
import time
import types

HERE = os.path.dirname(os.path.abspath(__file__))
RSS_LIMIT = 2200 << 20
TPC_TEST = "tests/test_executor.py::TestExecutor::test_execute_tpcds"


def _send(stream, obj):
    data = marshal.dumps(obj)
    stream.write(struct.pack("<I", len(data)) + data)
    stream.flush()


def _recv(stream):
    head = stream.read(4)
    if len(head) < 4:
        raise EOFError
    (size,) = struct.unpack("<I", head)
    data = stream.read(size)
    if len(data) < size:
        raise EOFError
    return marshal.loads(data)


class Zygote:
    """Parent-side handle. All methods fail closed: None means no information."""

    def __init__(self, base, ids, deadline):
        self.base = os.path.abspath(str(base))
        self.ids = list(ids)
        self.deadline = deadline
        self.process = None
        self.cpu = 0.0
        self.last_cpu = 0.0
        self.failed = False

    def start(self):
        if self.process is not None or self.failed:
            return self.process is not None
        env = dict(os.environ)
        env["PYTHONPATH"] = HERE
        # Plan digests are recorded with hash seed 0; some plans follow string-set order.
        env["PYTHONHASHSEED"] = "0"
        try:
            self.process = subprocess.Popen(
                [sys.executable, "-B", "-c", "import zexec; zexec.serve()"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=None if os.environ.get("ZEXEC_DEBUG") else subprocess.DEVNULL,
                cwd=HERE, env=env)
            _send(self.process.stdin, (self.base, self.ids))
            ready = self._wait(60.0)
            if ready != "ready":
                raise OSError("zygote failed")
        except (OSError, EOFError, ValueError):
            self.close()
            self.failed = True
            return False
        return True

    def _wait(self, timeout):
        limit = min(self.deadline, time.monotonic() + timeout)
        fd = self.process.stdout.fileno()
        if not select.select([fd], [], [], max(0.0, limit - time.monotonic()))[0]:
            raise OSError("timeout")
        return _recv(self.process.stdout)

    def run(self, patch, tests, limits, budget, wall, first_failure=False):
        """Return (failed, done) test numbers, or None."""
        if not self.start():
            return None
        try:
            _send(self.process.stdin, ("tests!" if first_failure else "tests", patch,
                                       [self.ids[n] for n in tests],
                                       [float(limits[n]) for n in tests], float(budget), float(wall)))
            reply = self._wait(wall + 5.0)
        except (OSError, EOFError, ValueError):
            self.close()
            self.failed = True
            return None
        if reply is None:
            return None
        failed, done, cpu = reply
        self.cpu += cpu
        self.last_cpu = cpu
        number = {self.ids[n]: n for n in tests}
        return {number[t] for t in failed}, {number[t] for t in done}

    def cases(self, patch, test, wanted, limit, budget, wall):
        """Run one test with its SQL fixture loaders yielding only ``wanted`` ({file: [index]})
        items; files not listed yield every item. Return True if it passed, False if it
        failed, None when unknown."""
        self.last_cpu = 0.0
        if not self.start():
            return None
        try:
            _send(self.process.stdin, ("cases", patch, [self.ids[test]], [float(limit)],
                                       float(budget), float(wall), wanted))
            reply = self._wait(wall + 5.0)
        except (OSError, EOFError, ValueError):
            self.close()
            self.failed = True
            return None
        if reply is None:
            return None
        failed, done, cpu = reply
        self.cpu += cpu
        self.last_cpu = cpu
        if self.ids[test] not in done:
            return None
        return self.ids[test] not in failed

    def tpc(self, patch, queries, budget, wall, expected=(), rows=True):
        """Digests of the given (name, index) TPC queries on the patched code, or None. The
        replay stops after the first digest that differs from ``expected``."""
        if not self.start():
            return None
        try:
            _send(self.process.stdin, ("tpc" if rows else "tpcplan", patch, list(queries),
                                       list(expected), float(budget), float(wall)))
            reply = self._wait(wall + 60.0)
        except (OSError, EOFError, ValueError):
            self.close()
            self.failed = True
            return None
        if reply is None:
            return None
        digests, cpu = reply
        self.cpu += cpu
        self.last_cpu = cpu
        return digests

    def close(self):
        if self.process is not None:
            try:
                self.process.stdin.close()
            except OSError:
                pass
            try:
                _, status, usage = os.wait4(self.process.pid, 0)
                self.total_cpu = usage.ru_utime + usage.ru_stime
                self.process.returncode = status
            except (OSError, ChildProcessError):
                self.process.kill()
                self.process.wait()
            self.process = None


# ---------------------------------------------------------------- server side

FIXTURE_LOADERS = ("load_sql_fixture_pairs", "load_sql_fixtures")


def _filter_fixtures(base, wanted):
    """Make the SQL fixture loaders of tests/helpers.py, wherever test modules bound them,
    yield only the wanted items of the listed files (in the forked child only)."""
    helpers = os.path.join(base, "tests", "helpers.py")
    keep = {name: set(indexes) for name, indexes in wanted.items()}

    def wrap(orig):
        def loader(filename, *args, **kwargs):
            chosen = keep.get(filename)
            for i, item in enumerate(orig(filename, *args, **kwargs)):
                if chosen is None or i in chosen:
                    yield item
        return loader

    wrapped = {}
    for module in list(sys.modules.values()):
        for name, value in list(getattr(module, "__dict__", {}).items()):
            if isinstance(value, types.FunctionType) and value.__name__ in FIXTURE_LOADERS \
                    and value.__code__.co_filename == helpers:
                if value not in wrapped:
                    wrapped[value] = wrap(value)
                setattr(module, name, wrapped[value])
    if not wrapped:
        raise RuntimeError("no fixture loaders")


class _Source(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    def __init__(self, files):
        self.files = files  # module name -> (filename, text, is_package)

    def find_spec(self, fullname, path=None, target=None):
        entry = self.files.get(fullname)
        if entry is None:
            return None
        filename, _, package = entry
        return importlib.util.spec_from_file_location(
            fullname, filename, loader=self,
            submodule_search_locations=[os.path.dirname(filename)] if package else None)

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        filename, text, _ = self.files[module.__name__]
        exec(compile(text, filename, "exec", dont_inherit=True), module.__dict__)


def _module_name(path):
    parts = path[:-3].split("/")
    package = parts[-1] == "__init__"
    if package:
        parts = parts[:-1]
    return ".".join(parts), package


def _apply(base, patch, index):
    import zpatch
    from impact import patched, split_diff

    files = split_diff(patch, physical=True)
    serve = {}
    fixtures = {}
    for path, hunks in files.items():
        filename = os.path.join(base, path)
        with open(filename, encoding="utf-8", newline="") as f:
            old = f.read()
        new = patched(old, hunks, physical=True)
        if path.startswith("tests/fixtures/") and path.endswith(".sql"):
            fixtures[os.path.normpath(os.path.abspath(filename))] = new.replace("\r\n", "\n").replace("\r", "\n")
            continue
        result = zpatch.analyze(path, old, new, filename)
        name, package = _module_name(path)
        serve[name] = (filename, new, package)
        module = sys.modules.get(name)
        if module is None:
            continue
        for key, code in result["swaps"]:
            for func in index.get(key, ()):
                func.__code__ = code
        # Other additions and deletions are not applied; the stage replays only tests that
        # such edits cannot reach.
        plain = set(result["plain_removed"])
        for scope, name, key in result["removed"]:
            if (scope, name) in plain:
                _delete(base, module, scope, name, key, index)
        for scope, name, code, defaults, kwdefaults, annotations, doc in result["plain_added"]:
            _add(module, scope, name, code, defaults, kwdefaults, annotations, doc)
        for kind, qualname, doc in result["docs"]:
            if kind == "module":
                module.__doc__ = doc
            else:
                target = module
                for part in qualname.split("."):
                    target = getattr(target, part)
                target.__doc__ = doc
        for qualname, values, kw in result["defaults"]:
            funcs = [f for k, fs in index.items() if k[0] == path and k[1] == qualname for f in fs]
            if not funcs:
                raise LookupError(qualname)
            for func in funcs:
                code = func.__code__
                func.__defaults__ = tuple(values) if values else None
                names = code.co_varnames[code.co_argcount:code.co_argcount + code.co_kwonlyargcount]
                kwd = {n: v for n, v in zip(names, kw) if v is not None}
                func.__kwdefaults__ = kwd or None
        # A function swapped by docstring only keeps its new __doc__ in sync.
        for key, code in result["swaps"]:
            doc = code.co_consts[0] if code.co_consts and isinstance(code.co_consts[0], str) else None
            for func in index.get(key, ()):
                if func.__doc__ != doc and doc is not None:
                    func.__doc__ = doc
    sys.meta_path.insert(0, _Source(serve))
    if fixtures:
        _serve_fixtures(base, fixtures)


def _delete(base, module, scope, name, key, index):
    """Delete a plain method from its class, as the patched class body would leave it out.
    Raise unless the class dict holds exactly that function and nothing else refers to it
    (a cache, alias or bound method made before the fork would keep the old behavior)."""
    target = module
    for part in scope.split("."):
        target = vars(target)[part]
    if not isinstance(target, type) or type(target).__delattr__ is not type.__delattr__:
        raise LookupError(scope)
    func = vars(target).get(name)
    code = getattr(func, "__code__", None)
    if not isinstance(func, types.FunctionType) or code.co_filename != os.path.join(base, key[0]) \
            or (code.co_qualname, code.co_firstlineno) != (key[1], key[2]):
        raise LookupError(key)
    delattr(target, name)
    # Frozen objects are invisible to get_referrers (unfreeze and freeze only splice lists).
    lists = set()
    for group in index.values():
        for other in group:
            if other is func:
                lists.add(id(group))
    gc.unfreeze()
    try:
        holders = gc.get_referrers(func)
    finally:
        gc.freeze()
    for holder in holders:
        if id(holder) in lists or isinstance(holder, types.FrameType):
            continue
        raise LookupError("referenced: " + type(holder).__name__)


def _add(module, scope, name, code, defaults, kwdefaults, annotations, doc):
    """Set a plain method on its class as the patched class body would define it. Raise
    unless the class and every class above or below it is made by type itself with no
    __init_subclass__ (a metaclass or hook could act on the class's members when it is made)."""
    target = module
    for part in scope.split("."):
        target = vars(target)[part]
    if not isinstance(target, type) or name in vars(target):
        raise LookupError(scope)
    seen, stack = set(), list(target.__mro__[:-1])
    while stack:
        cls = stack.pop()
        if cls in seen:
            continue
        seen.add(cls)
        if type(cls) is not type or "__init_subclass__" in vars(cls) \
                or "__getattr__" in vars(cls) or "__getattribute__" in vars(cls):
            raise LookupError(cls.__qualname__)
        stack.extend(type.__subclasses__(cls))
    if target.__mro__[-1] is not object or code.co_qualname != scope + "." + name:
        raise LookupError(scope)
    if code.co_freevars == ():
        closure = None
    elif code.co_freevars == ("__class__",):
        closure = (types.CellType(target),)
    else:
        raise LookupError(code.co_freevars)
    func = types.FunctionType(code, module.__dict__, name, defaults, closure)
    func.__qualname__ = code.co_qualname
    func.__kwdefaults__ = kwdefaults
    func.__annotations__ = annotations
    func.__doc__ = doc
    setattr(target, name, func)


def _serve_fixtures(base, texts):
    """Make the tests/helpers.py loaders read the patched text of SQL fixture files (in the
    forked child only). Other readers still see the pin; the stage replays only tests that
    read their fixtures through these loaders."""
    import builtins
    import io

    helpers = os.path.join(base, "tests", "helpers.py")
    modules = [m for m in list(sys.modules.values())
               if getattr(m, "__file__", None) and os.path.abspath(m.__file__) == helpers]
    if not modules:
        raise RuntimeError("tests/helpers.py not loaded")

    def opener(file, mode="r", *args, **kwargs):
        text = texts.get(os.path.normpath(os.path.abspath(file))) if isinstance(file, str) else None
        if text is None:
            return builtins.open(file, mode, *args, **kwargs)
        if mode not in ("r", "rt"):
            raise OSError("fixture opened for " + mode)
        return io.StringIO(text)

    for module in modules:
        module.open = opener


def _isolated(work):
    """(result, CPU-s) of ``work()`` in a forked child; result None if it raised."""
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(r)
        try:
            out = marshal.dumps(work())
        except BaseException:
            out = marshal.dumps(None)
        with os.fdopen(w, "wb") as f:
            f.write(out)
        os._exit(0)
    os.close(w)
    with os.fdopen(r, "rb") as f:
        data = f.read()
    _, _, usage = os.wait4(pid, 0)
    try:
        result = marshal.loads(data)
    except (EOFError, ValueError, TypeError):
        result = None
    return result, usage.ru_utime + usage.ru_stime


def serve():
    import zrun

    stdin, stdout = sys.stdin.buffer, sys.stdout.buffer
    base, ids = _recv(stdin)
    os.chdir(base)
    sys.path.insert(1, HERE)
    import zpatch  # noqa: F401  (import before forking)
    import impact  # noqa: F401
    if base not in sys.path:
        sys.path.insert(0, base)
    import sqlglot  # noqa: F401
    prefix = os.path.join(base, "sqlglot") + os.sep
    found = {}
    index = {}

    def reindex():
        index.clear()
        gc.unfreeze()
        for obj in gc.get_objects():
            if isinstance(obj, types.FunctionType):
                code = obj.__code__
                if code.co_filename.startswith(prefix):
                    key = (code.co_filename[len(base) + 1:], code.co_qualname, code.co_firstlineno)
                    index.setdefault(key, []).append(obj)
        gc.freeze()

    def ensure(test_ids):
        # Test modules are imported on first use, as collection would import them.
        missing = [t for t in test_ids if t not in found]
        if missing:
            more, _ = zrun.load(base, missing)
            found.update(more)
            reindex()

    reindex()
    runner = zrun.Runner(found)
    tpc_states = {}
    _send(stdout, "ready")
    while True:
        try:
            job = _recv(stdin)
        except EOFError:
            break
        kind, job = job[0], job[1:]
        first_failure = kind == "tests!"
        if first_failure:
            kind = "tests"
        if kind in ("tpc", "tpcplan"):
            if kind not in tpc_states:
                try:
                    ensure([TPC_TEST])
                    import ztpc_units
                    # Plan replays need the queries only, not the table rows.
                    tpc_states[kind] = ztpc_units.Tables(rows=kind == "tpc")
                    reindex()
                except Exception:
                    tpc_states[kind] = False
            tpc_state = tpc_states[kind]
            if tpc_state is False:
                _send(stdout, None)
                continue
        patch, tests, limits, budget, wall = job[:5]
        wanted = job[5] if kind == "cases" else None
        if kind in ("tests", "cases"):
            try:
                ensure(tests)
            except Exception:
                _send(stdout, None)
                continue
        r, w = os.pipe()
        pid = os.fork()
        if pid == 0:
            os.close(r)
            code = 1
            try:
                signal.signal(signal.SIGPIPE, signal.SIG_DFL)
                _apply(base, patch, index)
                if kind in ("tpc", "tpcplan"):
                    import ztpc_units
                    digests = []
                    begin = time.process_time()
                    used = 0.0
                    for k, (name, i) in enumerate(tests):
                        if time.process_time() + used - begin > budget:
                            break
                        # Queries run in one process: warm caches make this 2.5 times cheaper,
                        # and a digest that follows an earlier query only fails to match.
                        try:
                            if kind == "tpc":
                                digest = ztpc_units.run_query(tpc_state, name, i)
                            else:
                                digest = ztpc_units.split_query(tpc_state, name, i, rows=False)[0]
                        except Exception:
                            digest = None
                        if digest is None:
                            break
                        digests.append(digest)
                        if k < len(limits) and digests[-1] != limits[k]:
                            break
                    payload = marshal.dumps((digests, time.process_time() + used))
                else:
                    if wanted is not None:
                        _filter_fixtures(base, wanted)
                    runnable = [t for t in tests if t in found]
                    failed, done = runner.run(runnable, dict(zip(tests, limits)), stop_after=budget,
                                              first_failure=first_failure)
                    payload = marshal.dumps((sorted(failed), sorted(done), time.process_time()))
                code = 0
            except BaseException:
                if os.environ.get("ZEXEC_DEBUG"):
                    import traceback
                    traceback.print_exc()
                payload = marshal.dumps(None)
            with os.fdopen(w, "wb") as out:
                out.write(payload)
            os._exit(code)
        os.close(w)
        chunks = []
        limit = time.monotonic() + wall
        killed = False
        page = os.sysconf("SC_PAGE_SIZE")
        while True:
            left = limit - time.monotonic()
            if left <= 0:
                os.kill(pid, signal.SIGKILL)
                killed = True
                break
            if not select.select([r], [], [], min(left, 0.05))[0]:
                # Resident memory guard: the scorer allows 4 GiB for everything.
                try:
                    with open(f"/proc/{pid}/statm") as f:
                        resident = int(f.read().split()[1]) * page
                except (OSError, ValueError, IndexError):
                    resident = 0
                if resident > RSS_LIMIT:
                    os.kill(pid, signal.SIGKILL)
                    killed = True
                    break
                continue
            chunk = os.read(r, 1 << 20)
            if not chunk:
                break
            chunks.append(chunk)
        os.close(r)
        os.waitpid(pid, 0)
        reply = None
        if not killed and chunks:
            try:
                reply = marshal.loads(b"".join(chunks))
            except (EOFError, ValueError, TypeError):
                reply = None
        _send(stdout, reply)
