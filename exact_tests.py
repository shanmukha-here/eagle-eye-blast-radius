"""Bound complete pytest executions of cheap affected tests."""

import contextlib
import json
import multiprocessing
from mmap import ACCESS_READ, mmap
import os
import resource
import signal
import sys
import tempfile
import time
import unittest
from pathlib import Path
from struct import Struct

from decision import overlay
from cpu_budget import seconds
from impact import split_diff, UnknownChange


MICROSECONDS = Struct("<I")


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


class ShortCohort:
    def __init__(self, ids):
        self.ids = set(ids)
        self.phases = {}
        self.failed = False

    def pytest_collection_modifyitems(self, items):
        items[:] = [item for item in items if item.nodeid in self.ids]

    def pytest_runtest_logreport(self, report):
        if report.failed or report.skipped:
            self.failed = True
        elif report.passed:
            self.phases.setdefault(report.nodeid, set()).add(report.when)

    def complete(self):
        return not self.failed and self.ids == self.phases.keys() and all(
            phases == {"setup", "call", "teardown"} for phases in self.phases.values()
        )


def child(send, directory, ids, collect, cpu_cap, cohort=False):
    try:
        os.setsid()
        signal.signal(signal.SIGPROF, signal.SIG_DFL)
        signal.setitimer(signal.ITIMER_PROF, cpu_cap)
        with open(os.devnull, "w") as sink:
            os.dup2(sink.fileno(), 1)
            os.dup2(sink.fileno(), 2)
        os.chdir(directory)
        sys.path.insert(0, str(directory))
        if not collect and not cohort:
            send.send(run_unittests(ids))
            return
        import pytest

        options = ["-q", "-p", "no:cacheprovider", "--rootdir", str(directory)]
        if cohort:
            os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
            checks = ShortCohort(ids)
            paths = sorted({test.partition("::")[0] for test in ids})
            result = pytest.main([*options, "--basetemp", str(Path(directory) / "pytest-run"), *paths],
                                 plugins=[checks])
            if result == pytest.ExitCode.OK and checks.complete():
                send.send(ids)
            return
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
        self.profile = None
        profile = ready.with_name("isolated_cpu.bin")
        if profile.is_file():
            with profile.open("rb") as file:
                self.profile = mmap(file.fileno(), 0, access=ACCESS_READ)
        self.ctx = multiprocessing.get_context("fork")
        self.remaining = min(20.0, count * 0.4)
        self.collection_remaining = min(14.0, count * 0.35)
        self.cleanup_remaining = min(6.0, count * 0.12)
        self.attempted = set()
        self.passed = set()
        self.deadline = time.monotonic() + min(130.0, count * 2.3 + 3.0)
        if len(os.listdir("/proc/self/task")) != 1 or "sqlglot" in sys.modules:
            self.remaining = self.collection_remaining = self.cleanup_remaining = 0.0

    def refine(self, patch, plan, workspace=None, short_only=False, allowance=None):
        available = self.remaining if allowance is None else allowance
        remaining = min(available, self.deadline - time.monotonic())
        if remaining < 0.23 or not plan:
            return plan
        selected = {number for group in plan for number in group}
        weights = self.scheduler.durations
        if len(selected) > 64 or short_only and max(weights[n] for n in selected) >= 3000:
            return plan
        try:
            edits = split_diff(patch)
            if any(not path.startswith("sqlglot/") or path.endswith("/__init__.py") for path in edits):
                return plan
        except UnknownChange:
            return plan
        # A successful all-pass run can erase at least a worker's startup cost.
        names = tuple(sorted(self.nodes[n] for n in selected))
        cohort = not set(names) <= self.ready.keys()
        expected = sum(weights[n] for n in selected) / 1000
        if cohort:
            if not short_only or len(selected) > 16 or expected > 1.0:
                return plan
            expected += 0.28
        else:
            fixtures = {node.rsplit("::", 1)[0]: self.ready[node] for node in names}
            expected += sum(fixtures.values()) / 1000
        expected = max(expected, self.fixture_cpu(selected))
        if expected > min(2.55, self.scheduler.cost(plan) / 1450):
            return plan
        cap = min(remaining, self.scheduler.cost(plan) / 1250, max(0.9, 1.45 * expected + 0.65))
        result = self.run(edits, names, cap, collect=False, workspace=workspace, cohort=cohort,
                          reserved=allowance is not None)
        if result is not None and all(name in self.numbers for name in result):
            passed = {self.numbers[name] for name in result}
            if passed <= selected:
                self.passed.update(passed)
                if short_only and passed != selected:
                    return plan
                return self.scheduler.repack(selected - passed, plan)
        return plan

    def certify_small(self, patch, plan, certificate):
        """Use a separate allowance for plans that could lose their only worker."""
        if len(plan) != 1 or len(plan[0]) > 64 or self.cleanup_remaining < 0.30:
            return plan
        selected = plan[0]
        recorded = sum(self.scheduler.durations[n] for n in selected)
        if recorded > 1100:
            return plan
        names = tuple(sorted(self.nodes[n] for n in selected))
        if names in self.attempted:
            return plan
        direct = all(name in self.ready for name in names)
        if not direct and (len(names) > 16 or recorded > 500):
            return plan
        estimate = max(self.fixture_cpu(selected), recorded / 1000.0)
        if estimate > (0.74 if direct else 0.45) or not certificate(patch):
            return plan
        cap = min(self.cleanup_remaining, (0.94 if direct else 0.61), 0.49 * (2.0 + recorded / 1000.0))
        start = seconds()
        try:
            return self.refine(patch, plan, short_only=True, allowance=cap)
        finally:
            self.cleanup_remaining -= seconds() - start

    def certify_worker(self, patch, plan, certificate):
        """Check an affordable standalone cohort that could erase a worker."""
        if len(plan) < 2 or self.cleanup_remaining < 0.36:
            return plan
        if sum(map(len, plan)) > 288:
            return plan
        weights = self.scheduler.durations
        possible = []
        all_tests = {number for worker in plan for number in worker}
        original_cost = self.scheduler.cost(plan)
        for worker in plan:
            if len(worker) > 64 or any(self.nodes[n] not in self.ready for n in worker):
                continue
            names = tuple(sorted(self.nodes[n] for n in worker))
            if names in self.attempted:
                continue
            estimate = max(self.fixture_cpu(worker), sum(weights[n] for n in worker) / 1000.0)
            if estimate > 0.54:
                continue
            reduced = self.scheduler.repack(all_tests.difference(worker), plan)
            savings = original_cost - self.scheduler.cost(reduced)
            if savings >= 1500:
                possible.append((savings - 1000 * estimate, estimate, worker))
        if not possible or not certificate(patch):
            return plan
        _, estimate, worker = max(possible)
        cap = min(self.cleanup_remaining, 0.88, 1.35 * estimate + 0.40)
        start = seconds()
        try:
            revised = self.refine(patch, [worker], allowance=cap)
            kept = all_tests.difference(worker)
            kept.update(n for group in revised for n in group)
            return self.scheduler.repack(kept, plan) if kept != all_tests else plan
        finally:
            self.cleanup_remaining -= seconds() - start

    def fixture_cpu(self, selected):
        """Estimate a fresh class fixture even when a test itself is cheap."""
        if self.profile is None:
            return 0.0
        classes = {}
        for number in selected:
            bit = self.scheduler.bits[number]
            start = (bit.bit_length() - 1) * MICROSECONDS.size
            if start < 0 or start + MICROSECONDS.size > len(self.profile):
                continue
            cost, = MICROSECONDS.unpack_from(self.profile, start)
            fixture = self.nodes[number].rsplit("::", 1)[0]
            classes[fixture] = max(classes.get(fixture, 0), cost)
        return sum(classes.values()) / 1e6

    def collection(self, patch, plan):
        cap = min(self.collection_remaining, self.deadline - time.monotonic(), 3.4)
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

    def run(self, edits, names, cap, collect, workspace=None, cohort=False, reserved=False):
        if not reserved:
            cap = getattr(self, "cpu_limit", float)(cap)
        if cap < (0.5 if collect else 0.23):
            return None
        started = time.monotonic()

        def used():
            a = resource.getrusage(resource.RUSAGE_SELF)
            b = resource.getrusage(resource.RUSAGE_CHILDREN)
            return a.ru_utime + a.ru_stime + b.ru_utime + b.ru_stime

        baseline = used()
        try:
            if collect or cohort:
                # Warm only the testing machinery. SQLGlot stays cold in each child.
                import pytest

                if pytest.__version__.split(".")[0] != "9" or "sqlglot" in sys.modules:
                    return None
            owned = workspace is None
            directory = tempfile.TemporaryDirectory() if owned else contextlib.nullcontext(workspace.root())
            with directory as current:
                root = Path(current)
                if owned:
                    overlay(self.root, root, edits, validate=not collect)
                with contextlib.ExitStack() as stack:
                    recv, send = self.ctx.Pipe(duplex=False)
                    stack.enter_context(recv)
                    stack.enter_context(send)
                    available = cap - (used() - baseline)
                    if available <= 0.04:
                        return None
                    run = self.ctx.Process(target=child, args=(send, root, names, collect, available, cohort))
                    if not collect:
                        self.attempted.add(names)
                    run.start()
                    send.close()
                    try:
                        limit = min(self.deadline, started + cap * 1.75 + 0.15)
                        wait = max(0, limit - time.monotonic())
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
            if collect:
                self.collection_remaining -= used() - baseline
            elif not reserved:
                self.remaining -= used() - baseline
        return None
