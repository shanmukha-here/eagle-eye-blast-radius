"""Attribute generator construction calls to the actual upstream memory subtests."""
import unittest
from contextlib import contextmanager
import trace_cases as trace
from trace_cases import pytest_configure, pytest_sessionfinish

original = unittest.TestCase.subTest


@contextmanager
def subtest(self, *args, **kwargs):
    if self.id().endswith(".test_generator_memory") and "dialect" in kwargs:
        trace.switch("dialect:" + kwargs["dialect"].value)
        try:
            with original(self, *args, **kwargs):
                yield
        finally:
            trace.switch("outside")
    else:
        with original(self, *args, **kwargs):
            yield


def install():
    trace.install()
    unittest.TestCase.subTest = subtest
