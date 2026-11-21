"""Index bare unittest tests whose pytest setup uses no additional fixtures."""

import importlib
import json
import resource
import time
import sys
import unittest
import zlib
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=None)
def load_module(path):
    module = importlib.import_module(path[:-3].replace("/", "."))
    if (getattr(module, "pytestmark", None) or hasattr(module, "setUpModule")
            or hasattr(module, "tearDownModule") or any(
                hasattr(value, "_fixture_function_marker") or hasattr(value, "_pytestfixturefunction")
                for value in vars(module).values()
            )):
        return None
    return module


def stock_case(cls):
    if not isinstance(cls, type) or not issubclass(cls, unittest.TestCase):
        return False
    return not any(getattr(parent, "pytestmark", None) or any(
        hasattr(value, "_fixture_function_marker") or hasattr(value, "_pytestfixturefunction")
        for value in vars(parent).values()
    ) for parent in cls.__mro__)


@lru_cache(maxsize=None)
def fixture_cost(cls):
    if (getattr(cls.setUpClass, "__func__", None) is unittest.TestCase.setUpClass.__func__
            and getattr(cls.tearDownClass, "__func__", None) is unittest.TestCase.tearDownClass.__func__):
        return 0
    started = time.monotonic()
    before = resource.getrusage(resource.RUSAGE_SELF)
    try:
        try:
            cls.setUpClass()
            cls.tearDownClass()
        finally:
            cls.doClassCleanups()
    except Exception:
        return None
    done = resource.getrusage(resource.RUSAGE_SELF)
    milliseconds = round(1000 * max(done.ru_utime + done.ru_stime - before.ru_utime - before.ru_stime,
                                    time.monotonic() - started))
    return milliseconds if milliseconds <= 450 else None


def supported(test):
    parts = test.split("::")
    if len(parts) != 3 or "[" in parts[-1]:
        return None
    path, family, name = parts
    module = load_module(path)
    cls = getattr(module, family, None) if module else None
    if not stock_case(cls) or getattr(cls, "__unittest_skip__", False):
        return None
    method = getattr(cls, name, None)
    if (not callable(method) or getattr(method, "pytestmark", None)
            or getattr(method, "__unittest_skip__", False)
            or getattr(method, "__unittest_expecting_failure__", False)):
        return None
    return fixture_cost(cls)


def build(path, destination, root):
    if any((root / "tests").rglob("conftest.py")):
        raise ValueError("cannot certify bare unittest under local pytest fixtures")
    tests = json.loads(zlib.decompress(path.read_bytes()))["ids"]
    selected = {test: cost for test in tests if (cost := supported(test)) is not None}
    destination.write_text(json.dumps(selected, separators=(",", ":")))


if __name__ == "__main__":
    build(*(Path(x) for x in sys.argv[1:]))
