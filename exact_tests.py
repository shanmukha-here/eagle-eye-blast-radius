"""Bound complete pytest executions of cheap affected tests."""

import contextlib
import multiprocessing
import os
import signal
import sys
import tempfile
import time
from pathlib import Path

from decision import overlay
from impact import split_diff, UnknownChange


class Outcomes:
    def __init__(self):
        self.phases = {}
        self.uncertain = set()

    def pytest_runtest_logreport(self, report):
        if report.passed and not hasattr(report, "wasxfail"):
            self.phases.setdefault(report.nodeid, set()).add(report.when)
        else:
            self.uncertain.add(report.nodeid)

    def pytest_collectreport(self, report):
        if report.failed:
            self.uncertain.add(None)

    def passed(self):
        if None in self.uncertain:
            return []
        complete = {"setup", "call", "teardown"}
        return sorted(node for node, phases in self.phases.items()
                      if phases == complete and node not in self.uncertain)


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
        import pytest

        options = ["-q", "-p", "no:cacheprovider", "--rootdir", str(directory)]
        if collect:
            checks = Collection()
            options += ["--collect-only", "--continue-on-collection-errors", "tests"]
        else:
            checks = Outcomes()
            options += ["--", *ids]
        result = pytest.main(
            options,
            plugins=[checks],
        )
        if collect and checks.ids is not None:
            send.send(checks.ids)
        elif not collect and result in (pytest.ExitCode.OK, pytest.ExitCode.TESTS_FAILED):
            send.send(checks.passed())
    except (Exception, SystemExit):
        pass
    finally:
        send.close()


class ExactTests:
    def __init__(self, scheduler, definitions, base, count):
        self.scheduler = scheduler
        self.nodes = {r["number"]: r["id"] for r in definitions}
        self.numbers = {v: k for k, v in self.nodes.items()}
        self.root = base.resolve()
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
        if len(selected) > 180 or sum(weights[n] for n in selected) > 600:
            return plan
        try:
            edits = split_diff(patch)
            if any(not path.startswith("sqlglot/") or path.endswith("/__init__.py") for path in edits):
                return plan
        except UnknownChange:
            return plan
        # A successful all-pass run can erase at least a worker's startup cost.
        cap = min(remaining, self.scheduler.cost(plan) / 3000, 1.3)
        names = sorted(self.nodes[n] for n in selected)
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
            # Warm only the testing machinery. Each child imports SQLGlot
            # from its own overlay before collecting any tests.
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
