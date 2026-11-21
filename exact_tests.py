"""Bound complete pytest executions of cheap affected tests."""

import contextlib
import json
import multiprocessing
import os
import signal
import sys
import tempfile
import time
import unittest
from pathlib import Path

from decision import overlay
from impact import split_diff, UnknownChange


class UnitOutcomes(unittest.TestResult):
    def __init__(self):
        super().__init__()
        self.passed = set()

    def addSuccess(self, test):
        self.passed.add(test.id())
        super().addSuccess(test)


def run_unittests(ids):
    loader = unittest.TestLoader()
    test_names = {}
    tests = unittest.TestSuite()
    for node in ids:
        path, *parts = node.split("::")
        name = ".".join([path[:-3].replace("/", "."), *parts])
        group = loader.loadTestsFromName(name)
        if group.countTestCases() != 1:
            return []
        test_names[name] = node
        tests.addTest(group)
    result = UnitOutcomes()
    result.failfast = True
    tests.run(result)
    # Fixture errors can be emitted after otherwise successful methods.
    if any(not isinstance(test, unittest.TestCase) for test, _ in (*result.errors, *result.skipped)):
        return []
    return [test_names[name] for name in result.passed if name in test_names]


class Collection:
    def __init__(self):
        self.ids = None

    def pytest_collection_finish(self, session):
        self.ids = [item.nodeid for item in session.items]


def child(send, directory, ids, collect):
    try:
        os.setsid()
        with open(os.devnull, "w") as sink:
            os.dup2(sink.fileno(), 1)
            os.dup2(sink.fileno(), 2)
        os.chdir(directory)
        sys.path.insert(0, str(directory))
        if not collect:
            send.send(run_unittests(ids))
            return
        import pytest

        options = ["-q", "-p", "no:cacheprovider", "--rootdir", str(directory)]
        checks = Collection()
        options += ["--collect-only", "--continue-on-collection-errors", "tests"]
        pytest.main(options, plugins=[checks])
        if checks.ids is not None:
            send.send(checks.ids)
    except (Exception, SystemExit):
        pass
    finally:
        send.close()


class ExactTests:
    def __init__(self, scheduler, definitions, base, count, ready):
        self.scheduler = scheduler
        self.nodes = {r["number"]: r["id"] for r in definitions}
        self.numbers = {v: k for k, v in self.nodes.items()}
        self.root = base.resolve()
        self.ready = json.loads(ready.read_bytes())
        self.ctx = multiprocessing.get_context("fork")
        self.remaining = min(25.0, count * 0.44)
        self.deadline = time.monotonic() + min(130.0, count * 2.3 + 3.0)
        if len(os.listdir("/proc/self/task")) != 1 or "sqlglot" in sys.modules:
            self.remaining = 0.0

    def refine(self, patch, plan):
        remaining = min(self.remaining, self.deadline - time.monotonic())
        if remaining < 0.23 or not plan:
            return plan
        selected = {number for group in plan for number in group}
        weights = self.scheduler.durations
        if len(selected) > 64:
            return plan
        try:
            edits = split_diff(patch)
            if any(not path.startswith("sqlglot/") or path.endswith("/__init__.py") for path in edits):
                return plan
        except UnknownChange:
            return plan
        # A successful all-pass run can erase at least a worker's startup cost.
        names = sorted(self.nodes[n] for n in selected)
        if not set(names) <= self.ready.keys():
            return plan
        fixtures = {node.rsplit("::", 1)[0]: self.ready[node] for node in names}
        expected = (sum(weights[n] for n in selected) + sum(fixtures.values())) / 1000
        if expected > 2.55:
            return plan
        cap = min(remaining, self.scheduler.cost(plan) / 1250, max(0.9, 1.45 * expected + 0.65))
        result = self.run(edits, names, cap, collect=False)
        if result is not None and all(name in self.numbers for name in result):
            passed = {self.numbers[name] for name in result}
            if passed <= selected:
                return self.scheduler.repack(selected - passed, plan)
        return plan

    def collection(self, patch, plan):
        cap = min(self.remaining, self.deadline - time.monotonic(), 1.8)
        if cap < 0.5:
            return None
        try:
            edits = split_diff(patch)
        except UnknownChange:
            return None
        ids = self.run(edits, [], cap, collect=True)
        if ids is None:
            return None
        present = set(ids)
        failed = {n for n, node in self.nodes.items() if node not in present}
        return self.scheduler.repack(failed | {n for w in plan for n in w}, plan)

    def run(self, edits, names, cap, collect):
        started = time.monotonic()
        try:
            if collect:
                # Warm only the testing machinery. SQLGlot stays cold in each child.
                import pytest

                if pytest.__version__.split(".")[0] != "9" or "sqlglot" in sys.modules:
                    return None
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                overlay(self.root, root, edits, validate=not collect)
                with contextlib.ExitStack() as stack:
                    recv, send = self.ctx.Pipe(duplex=False)
                    stack.enter_context(recv)
                    stack.enter_context(send)
                    run = self.ctx.Process(target=child, args=(send, root, names, collect))
                    run.start()
                    send.close()
                    try:
                        wait = max(0, cap - (time.monotonic() - started))
                        if recv.poll(wait):
                            return recv.recv()
                    finally:
                        run.join(0.01)
                        if run.is_alive():
                            with contextlib.suppress(ProcessLookupError):
                                if os.getpgid(run.pid) == run.pid:
                                    os.killpg(run.pid, signal.SIGKILL)
                                else:
                                    run.kill()
                            run.join()
        except (EOFError, KeyError, OSError, SyntaxError, UnknownChange, ValueError):
            pass
        finally:
            self.remaining -= time.monotonic() - started
        return None
