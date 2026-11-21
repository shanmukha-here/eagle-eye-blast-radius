"""Index bare unittest tests whose pytest setup uses no additional fixtures."""

import importlib
import json
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
    if (getattr(cls.setUpClass, "__func__", None) is not unittest.TestCase.setUpClass.__func__
            or getattr(cls.tearDownClass, "__func__", None) is not unittest.TestCase.tearDownClass.__func__):
        return False
    return not any(getattr(parent, "pytestmark", None) or any(
        hasattr(value, "_fixture_function_marker") or hasattr(value, "_pytestfixturefunction")
        for value in vars(parent).values()
    ) for parent in cls.__mro__)


def supported(test):
    parts = test.split("::")
    if len(parts) != 3 or "[" in parts[-1]:
        return False
    path, family, name = parts
    module = load_module(path)
    cls = getattr(module, family, None) if module else None
    if not stock_case(cls):
        return False
    method = getattr(cls, name, None)
    return (callable(method) and not getattr(method, "pytestmark", None)
            and not getattr(method, "__unittest_skip__", False)
            and not getattr(method, "__unittest_expecting_failure__", False))


def build(path, destination, root):
    if any((root / "tests").rglob("conftest.py")):
        raise ValueError("cannot certify bare unittest under local pytest fixtures")
    tests = json.loads(zlib.decompress(path.read_bytes()))["ids"]
    selected = [test for test in tests if supported(test)]
    destination.write_text(json.dumps(selected, separators=(",", ":")))


if __name__ == "__main__":
    build(*(Path(x) for x in sys.argv[1:]))
