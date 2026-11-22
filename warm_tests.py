"""Reuse imports when a changed function has never run during preload."""

from collections import defaultdict
from contextlib import suppress
import gc
import importlib
import os
from pathlib import Path
import signal
import sys
import time
from types import AsyncGeneratorType, CodeType, CoroutineType, FunctionType, GeneratorType

from cpu_budget import seconds
from exact_tests import run_unittests


def descendants(code):
    for value in code.co_consts:
        if isinstance(value, CodeType):
            yield value
            yield from descendants(value)


def load(base, modules):
    prefix = str(base) + "/"
    called = set()
    monitor = sys.monitoring
    tool = next((index for index in range(5, -1, -1) if monitor.get_tool(index) is None), None)
    if tool is None:
        return None, None

    def start(code, offset):
        file = code.co_filename
        if file.startswith(prefix + "sqlglot/"):
            called.add((file.removeprefix(prefix), code.co_qualname))
        return monitor.DISABLE

    monitor.use_tool_id(tool, "patched test preload")
    event = monitor.events.PY_START
    monitor.register_callback(tool, event, start)
    monitor.set_events(tool, event)
    try:
        for module in modules:
            importlib.import_module(module)
    finally:
        monitor.set_events(tool, 0)
        monitor.register_callback(tool, event, None)
        monitor.free_tool_id(tool)

    functions = defaultdict(list)
    for value in gc.get_objects():
        if isinstance(value, FunctionType):
            code = value.__code__
            if code.co_filename.startswith(prefix + "sqlglot/"):
                functions[code.co_filename.removeprefix(prefix), code.co_qualname].append(value)
        elif isinstance(value, (GeneratorType, CoroutineType, AsyncGeneratorType)):
            if isinstance(value, GeneratorType):
                code = value.gi_code
            elif isinstance(value, CoroutineType):
                code = value.cr_code
            else:
                code = value.ag_code
            if code.co_filename.startswith(prefix + "sqlglot/"):
                called.add((code.co_filename.removeprefix(prefix), code.co_qualname))
    return called, functions


def install(root, changes, called, functions):
    for path, names in changes.items():
        file = root / path
        tree = compile(file.read_bytes(), str(file), "exec", dont_inherit=True)
        definitions = defaultdict(list)
        for code in descendants(tree):
            definitions[code.co_qualname].append(code)
        for name in names:
            if any(file == path and (qual == name or qual.startswith(name + ".<locals>."))
                   for file, qual in called):
                return False
            instances = functions.get((path, name))
            revised = definitions[name]
            if not instances or len(revised) != 1:
                return False
            code = revised[0]
            if any(value.__code__.co_freevars != code.co_freevars for value in instances):
                return False
            for value in instances:
                value.__code__ = code
    return True


def fork_job(conn, root, names, changes, called, functions, cap):
    started = seconds()
    os.setsid()
    signal.signal(signal.SIGPROF, signal.SIG_DFL)
    signal.setitimer(signal.ITIMER_PROF, cap)
    passed = None
    try:
        os.chdir(root)
        sys.path.insert(0, str(root))
        updated = install(root, changes, called, functions)
        imports = seconds() - started
        if updated:
            passed = run_unittests(names, continue_after_failure=True)
            conn.send((passed, imports, seconds() - started - imports))
    except (Exception, SystemExit):
        pass
    finally:
        conn.close()
        os._exit(0)


def server(duplex, base, modules):
    startup = seconds()
    try:
        os.setsid()
        with open(os.devnull, "w") as sink:
            os.dup2(sink.fileno(), 1)
            os.dup2(sink.fileno(), 2)
        os.chdir(base)
        sys.path.insert(0, str(base))
        called, functions = load(base, modules)
        if called is None or len(os.listdir("/proc/self/task")) != 1:
            duplex.send(None)
            return
        duplex.send(seconds() - startup)
        while True:
            request = duplex.recv()
            if request is None:
                return
            root, names, changes, cap = request
            started = seconds()
            recv, send = duplex_context().Pipe(duplex=False)
            with recv, send:
                pid = os.fork()
                if not pid:
                    recv.close()
                    duplex.close()
                    fork_job(send, root, names, changes, called, functions, cap)
                send.close()
                result = None
                try:
                    if recv.poll(0.3 + 2 * cap):
                        result = recv.recv()
                except EOFError:
                    pass
                finally:
                    try:
                        os.killpg(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        with suppress(ProcessLookupError):
                            os.kill(pid, signal.SIGKILL)
                    os.waitpid(pid, 0)
            duplex.send((result, seconds() - started))
    except (Exception, SystemExit):
        pass
    finally:
        duplex.close()
        os._exit(0)


def duplex_context():
    import multiprocessing

    return multiprocessing.get_context("fork")


class WarmTests:
    def __init__(self, base, nodes=(), dependencies=()):
        self.base = base
        test_modules = sorted({node.partition("::")[0][:-3].replace("/", ".") for node in nodes})
        self.modules = ("sqlglot", *dependencies, *test_modules)
        self.proc = self.conn = None
        self.disabled = False

    def run(self, root, names, changes, cap, deadline):
        if self.disabled:
            return None, 0.0, 0.0
        startup = 0.0
        try:
            if self.proc is None:
                self.conn, child = duplex_context().Pipe()
                self.proc = duplex_context().Process(target=server, args=(child, self.base, self.modules))
                with child:
                    self.proc.start()
                if not self.conn.poll(min(5.0, max(0.0, deadline - time.monotonic()))):
                    raise TimeoutError
                startup = self.conn.recv()
                if startup is None:
                    raise RuntimeError("warm imports unavailable")
            self.conn.send((root, names, changes, cap))
            if not self.conn.poll(min(0.6 + cap * 2, max(0.0, deadline - time.monotonic()))):
                raise TimeoutError
            result, cost = self.conn.recv()
            return result, cost, startup
        except (EOFError, OSError, RuntimeError):
            self.disabled = True
            self.close()
            return None, 0.0, startup or 0.0

    def close(self):
        if self.conn is not None:
            with suppress(OSError):
                self.conn.send(None)
            self.conn.close()
            self.conn = None
        if self.proc is not None:
            self.proc.join(0.15)
            if self.proc.is_alive():
                self.proc.kill()
                self.proc.join()
            self.proc = None
