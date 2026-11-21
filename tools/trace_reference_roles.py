"""Distinguish clean field reads in exact reference SQL from the main test."""
import json
import os
import sys
from functools import wraps
from pathlib import Path

import pytest
import trace_access as trace
from trace_access import pytest_runtest_setup, pytest_runtest_call, pytest_runtest_teardown

# The built-in context hooks and pool shim also account for work in subprocesses.
# Put this before patched-loader exec so startup reads are included.
def configure_before_spawn():
    trace.install()


class Intent:
    def __init__(self, original, prefix):
        self.original = original
        self.prefix = prefix

    def __call__(self, *args, **kwargs):
        parent = trace.owner
        trace.owner = f"{self.prefix}:{parent}"
        try:
            return self.original(*args, **kwargs)
        finally:
            trace.owner = parent


def pytest_configure(config):
    trace.pytest_configure(config)


def pytest_collection_finish(session):
    from sqlglot import transpile
    from tests import test_executor

    own = test_executor.TestExecutor
    original = own.cached_execute

    @wraps(original)
    def reference(self, *args, **kwargs):
        return Intent(original, "reference")(self, *args, **kwargs)

    own.cached_execute = reference
    test_executor.transpile = Intent(transpile, "reference")


def pytest_sessionfinish(session, exitstatus):
    if exitstatus:
        return
    trace.pytest_sessionfinish(session, exitstatus)
    Path(os.environ["TRACE_OUTPUT"], "refs-complete.json").write_text(
        json.dumps([item.nodeid for item in session.items])
    )
