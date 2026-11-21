"""Cold-import a structurally verified literal edit and the built-in dialect registry."""

import importlib
import importlib.abc
import importlib.util
import json
import os
import signal
from struct import Struct
import sys
from pathlib import Path


REQUEST = Struct("<II")
RESPONSE = Struct("<Bd")
FAILED, PASSED, UNSUPPORTED = range(3)


class PatchedModule(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    def __init__(self, module, filename, source):
        self.module = module
        self.filename = filename
        self.source = source

    def find_spec(self, fullname, path, target=None):
        if fullname == self.module:
            options = {}
            if Path(self.filename).name == "__init__.py":
                options["submodule_search_locations"] = [str(Path(self.filename).parent)]
            return importlib.util.spec_from_file_location(fullname, self.filename, loader=self, **options)
        return None

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        exec(compile(self.source, self.filename, "exec"), module.__dict__)


def module_name(path):
    return path[:-3].replace("/", ".").removesuffix(".__init__")


class PrefixImports(importlib.abc.MetaPathFinder):
    def __init__(self):
        self.names = set()

    def find_spec(self, fullname, path=None, target=None):
        self.names.add(fullname)
        return None


def serve(root):
    from resource import RUSAGE_CHILDREN, RUSAGE_SELF, getrusage

    prefix = os.path.abspath(root) + "/"
    seen = set()
    collecting = True

    def record(event, args):
        if collecting and event in ("open", "compile"):
            filename = args[0] if event == "open" else args[1]
            if isinstance(filename, (str, bytes)):
                path = os.path.abspath(os.fsdecode(filename))
                if path.startswith(prefix):
                    seen.add(path)

    sys.addaudithook(record)
    monitor = PrefixImports()
    sys.meta_path.insert(0, monitor)
    sys.path.insert(0, root)
    output = os.dup(sys.stdout.fileno())
    with open(os.devnull, "w") as sink:
        os.dup2(sink.fileno(), sys.stdout.fileno())
    from sqlglot.dialects import Dialect

    sys.meta_path.remove(monitor)
    collecting = False
    covered = monitor.names | sys.modules.keys()
    single_thread = len(os.listdir("/proc/self/task")) == 1
    stream = sys.stdin.buffer
    while data := stream.read(REQUEST.size):
        path_size, source_size = REQUEST.unpack(data)
        if not 0 < path_size <= 1024 or not 0 < source_size <= 1_500_000:
            return
        path = stream.read(path_size).decode()
        source = stream.read(source_size)
        if len(source) != source_size:
            return
        module = module_name(path)
        filename = os.path.join(root, path)
        verdict = UNSUPPORTED
        if (single_thread and path.startswith("sqlglot/") and module not in covered
                and filename not in seen):
            pid = os.fork()
            if pid == 0:
                try:
                    os.close(output)
                    signal.signal(signal.SIGALRM, signal.SIG_DFL)
                    signal.setitimer(signal.ITIMER_REAL, 1.30)
                    sys.meta_path.insert(0, PatchedModule(module, filename, source))
                    importlib.import_module(module)
                    list(Dialect.classes.values())
                except BaseException:
                    os._exit(FAILED)
                os._exit(PASSED)
            _, status = os.waitpid(pid, 0)
            if os.WIFEXITED(status):
                verdict = os.WEXITSTATUS(status)
        current = getrusage(RUSAGE_SELF)
        children = getrusage(RUSAGE_CHILDREN)
        elapsed = current.ru_utime + current.ru_stime + children.ru_utime + children.ru_stime
        os.write(output, RESPONSE.pack(verdict, elapsed))
    os.close(output)


if __name__ == "__main__":
    root, path, *options = sys.argv[1:]
    if path == "--serve":
        serve(root)
        raise SystemExit(0)
    module = module_name(path)
    source = sys.stdin.buffer.read()
    filename = str(Path(root) / path)
    called = set()

    def trace(frame, event, argument):
        if event == "call" and frame.f_code.co_filename == filename:
            called.add(frame.f_code.co_name)

    if options == ["--trace"]:
        sys.setprofile(trace)
    sys.meta_path.insert(0, PatchedModule(module, filename, source))
    sys.path.insert(0, root)
    from sqlglot.dialects import Dialect

    importlib.import_module(module)
    # Force lazy registration and construction of inherited dialect classes.
    list(Dialect.classes.values())
    if options == ["--trace"]:
        sys.setprofile(None)
        print(json.dumps(sorted(called)))
