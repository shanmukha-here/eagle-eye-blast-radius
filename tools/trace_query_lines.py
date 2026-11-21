"""Record precise query and optimizer contexts of executed source lines on the clean pin."""
import sys
from pathlib import Path

import trace_cases as trace
from trace_cases import pytest_configure, pytest_sessionfinish  # noqa: F401


def line(code, number):
    name = code.co_filename
    if name.startswith(trace.ROOT + "sqlglot/"):
        key = f"{name[len(trace.ROOT):]}:{code.co_firstlineno}:{code.co_qualname}@{number}"
        trace.coverage[key].add(trace.context)
    return trace.MON.DISABLE


def install():
    trace.install()
    trace.MON.register_callback(trace.TOOL, trace.MON.events.LINE, line)
    trace.MON.set_events(trace.TOOL, trace.MON.events.PY_START | trace.MON.events.LINE)
