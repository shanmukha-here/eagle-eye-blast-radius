"""Run numbered unittest methods in the current process with pytest's failure rules."""

import importlib
import signal
import sys
import time
import unittest


class Timeout(BaseException):
    pass


def split_id(test_id):
    path, cls, method = test_id.split("::")
    return path[:-3].replace("/", "."), cls, method


def preload_all(root):
    """Import every sqlglot module so tests never import source lazily."""
    import pkgutil
    if root not in sys.path:
        sys.path.insert(0, root)
    import sqlglot
    failed = []
    for info in pkgutil.walk_packages(sqlglot.__path__, prefix="sqlglot."):
        if info.name == "sqlglot.__main__":
            continue
        try:
            importlib.import_module(info.name)
        except Exception:
            failed.append(info.name)
    return failed


class SyncExecutor:
    """In-process stand-in for ProcessPoolExecutor that keeps the pickle boundary."""

    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **_):
        if initializer is not None:
            initializer(*_roundtrip(initargs))

    def submit(self, fn, *args, **kwargs):
        from concurrent.futures import Future
        future = Future()
        try:
            value = fn(*_roundtrip(args), **_roundtrip(kwargs))
            future.set_result(_roundtrip(value))
        except Exception as e:
            future.set_exception(e)
        return future

    def map(self, fn, *iterables, **_):
        return [self.submit(fn, *args).result() for args in zip(*iterables)]

    def shutdown(self, wait=True, cancel_futures=False):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _roundtrip(value):
    import pickle
    return pickle.loads(pickle.dumps(value))


def install_sync_pools():
    for name, mod in list(sys.modules.items()):
        if (name == "tests" or name.startswith("tests.")) and getattr(mod, "ProcessPoolExecutor", None) is not None:
            mod.ProcessPoolExecutor = SyncExecutor


def load(root, ids):
    """Import the test modules of ``ids``. Return {id: (class, method)} and failed modules."""
    if root not in sys.path:
        sys.path.insert(0, root)
    found, broken = {}, set()
    for test_id in ids:
        module, cls, method = split_id(test_id)
        if module in broken:
            continue
        try:
            mod = importlib.import_module(module)
            klass = getattr(mod, cls)
            getattr(klass, method)
        except Exception:
            broken.add(module)
            continue
        found[test_id] = (klass, method)
    install_sync_pools()
    return found, broken


class Result(unittest.TestResult):
    def __init__(self, owner):
        super().__init__()
        self.owner = owner
        self.failed = set()
        self.class_failed = set()

    def _mark(self, test):
        test_id = self.owner.get(id(test))
        if test_id is not None:
            self.failed.add(test_id)
        else:
            # Class or module fixture error holders fail every test in scope.
            self.class_failed.add(str(test))

    def addError(self, test, err):
        self._mark(test)

    def addFailure(self, test, err):
        self._mark(test)

    def addSubTest(self, test, subtest, err):
        if err is not None:
            self._mark(test)

    def addUnexpectedSuccess(self, test):
        self._mark(test)

    def startTest(self, test):
        self.owner.start(test)

    def stopTest(self, test):
        self.owner.stop(test)


class Runner:
    """Run tests grouped by class, with a per-test wall limit raising Timeout."""

    def __init__(self, found):
        self.found = found
        self.ids = {}
        self.limits = {}
        self.current = None
        self.times = {}
        self.started = 0.0

    def get(self, key):
        return self.ids.get(key)

    def start(self, test):
        test_id = self.ids.get(id(test))
        self.current = test_id
        self.started = time.process_time()
        limit = self.limits.get(test_id)
        if limit:
            signal.setitimer(signal.ITIMER_REAL, limit, 0.05)

    def stop(self, test):
        signal.setitimer(signal.ITIMER_REAL, 0)
        if self.current is not None:
            self.times[self.current] = time.process_time() - self.started
        self.current = None

    def run(self, ids, limits=None, stop_after=None):
        """Return the set of ids that failed. ``stop_after`` is a CPU-second budget."""
        self.limits = limits or {}
        groups = {}
        order = []
        for test_id in ids:
            klass, method = self.found[test_id]
            if klass not in groups:
                groups[klass] = []
                order.append(klass)
            groups[klass].append((test_id, method))

        def alarm(signum, frame):
            raise Timeout()

        previous = signal.signal(signal.SIGALRM, alarm)
        failed = set()
        done = []
        begin = time.process_time()
        try:
            for klass in order:
                if stop_after is not None and time.process_time() - begin > stop_after:
                    break
                suite = unittest.TestSuite()
                members = []
                for test_id, method in groups[klass]:
                    case = klass(method)
                    self.ids[id(case)] = test_id
                    suite.addTest(case)
                    members.append(test_id)
                result = Result(self)
                try:
                    suite.run(result)
                except Timeout:
                    result.class_failed.add("timeout")
                finally:
                    signal.setitimer(signal.ITIMER_REAL, 0)
                if result.class_failed:
                    failed.update(members)
                failed.update(result.failed)
                done.extend(members)
                self.ids.clear()
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous)
        return failed, done
