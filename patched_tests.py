"""Certify individual short tests against a bounded patched checkout."""

from contextlib import suppress
import multiprocessing
import os
from pathlib import Path
import signal
import sys
import tempfile
import time

from cpu_budget import seconds
from decision import PatchWorkspace
from impact import UnknownChange, split_diff


PHASES = {"setup", "call", "teardown"}


class Outcomes:
    def __init__(self, names):
        self.names = set(names)
        self.phases = {}
        self.bad = set()
        self.bad_files = set()
        self.collection_error = False

    def pytest_collection_modifyitems(self, config, items):
        selected = [item for item in items if item.nodeid in self.names]
        deselected = [item for item in items if item.nodeid not in self.names]
        items[:] = selected
        config.hook.pytest_deselected(items=deselected)

    def pytest_collectreport(self, report):
        self.collection_error |= report.failed

    def pytest_runtest_logreport(self, report):
        node = report.nodeid
        if not report.passed or getattr(report, "wasxfail", None):
            self.bad.add(node)
            if report.when in ("setup", "teardown"):
                self.bad_files.add(node.partition("::")[0])
        elif report.when in PHASES:
            self.phases.setdefault(node, set()).add(report.when)

    def passed(self):
        if self.collection_error:
            return []
        return sorted(node for node, phases in self.phases.items()
                      if node in self.names and phases == PHASES and node not in self.bad
                      and node.partition("::")[0] not in self.bad_files)


def execute(conn, root, names, cpu_cap):
    try:
        start = time.process_time()
        os.setsid()
        signal.signal(signal.SIGPROF, signal.SIG_DFL)
        signal.setitimer(signal.ITIMER_PROF, cpu_cap)
        with open(os.devnull, "w") as sink:
            os.dup2(sink.fileno(), 1)
            os.dup2(sink.fileno(), 2)
        os.chdir(root)
        sys.path.insert(0, str(root))
        os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"

        import pytest

        # The parent warms only framework dependencies. Project and tests are
        # first imported here, with the patched checkout first on sys.path.
        import sqlglot
        if "sqlglot/__init__.py" != str(Path(sqlglot.__file__).relative_to(root)):
            return
        imported = time.process_time()
        result = Outcomes(names)
        paths = sorted({node.partition("::")[0] for node in names})
        status = pytest.main([
            "-q", "--tb=no", "-p", "no:cacheprovider", "-p", "pytest_timeout",
            "--timeout", "6", "--timeout-method", "signal",
            "--continue-on-collection-errors",
            "--rootdir", str(root), "--basetemp", str(root / "pytest-run"), *paths,
        ], plugins=[result])
        if status in (pytest.ExitCode.OK, pytest.ExitCode.TESTS_FAILED):
            conn.send((result.passed(), imported - start, time.process_time() - imported))
    except (Exception, SystemExit):
        pass
    finally:
        conn.close()


def stop(process):
    process.join(0.01)
    if process.is_alive():
        with suppress(ProcessLookupError):
            if os.getpgid(process.pid) == process.pid:
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        process.join()


class PatchedTests:
    def __init__(self, scheduler, profile, base, count):
        self.scheduler = scheduler
        self.profile = profile
        self.base = base.resolve()
        self.context = multiprocessing.get_context("fork")
        self.remaining = min(90.0, count * 1.8)
        self.deadline = time.monotonic() + (125 if count <= 40 else 185)
        self.available = len(os.listdir("/proc/self/task")) == 1
        self.stats = {}

    def refine(self, patch, plan, long_tests, known_passes):
        self.stats = {}
        if not plan or not self.available or self.remaining < 0.25:
            return plan
        if self.deadline - time.monotonic() < 1.0:
            return plan
        selected = {number for worker in plan for number in worker}
        short = {n for n in selected - long_tests
                 if self.scheduler.durations[n] < 3000 and self.scheduler.bits[n]}
        candidates = short - known_passes
        if not candidates:
            return plan
        after = self.scheduler.repack(selected - short, plan)
        upside = (self.scheduler.cost(plan) - self.scheduler.cost(after)) / 1000
        if upside < 0.18:
            return plan
        expected = max(0.10, 0.55 * sum(self.scheduler.durations[n] for n in candidates) / 1000,
                       self.profile.fixture_cpu(candidates))
        if expected > min(2.25, upside * 1.1):
            return plan
        try:
            edits = split_diff(patch)
            if not edits or any(
                not (path.startswith("sqlglot/") and path.endswith(".py")
                     or path.startswith("tests/fixtures/") and path.endswith(".sql"))
                for path in edits
            ):
                return plan
        except UnknownChange:
            return plan

        begin = seconds()
        cap = min(self.remaining, 3.0, max(0.55, expected * 1.25 + 0.35))
        names = tuple(sorted(self.profile.nodes[n] for n in candidates))
        self.stats = {"patched_candidates": len(names)}
        try:
            # A new import for every child avoids aliased objects from the pin.
            # Importing pytest once amortizes framework startup across commits.
            import pytest

            if any(name in sys.modules for name in ("sqlglot", "tests")):
                return plan
            with PatchWorkspace(self.base, patch) as workspace:
                root = workspace.root()
                overlay_cpu = seconds() - begin
                if cap - overlay_cpu < 0.15:
                    return plan
                recv, send = self.context.Pipe(duplex=False)
                with recv, send:
                    process = self.context.Process(target=execute, args=(send, root, names, cap - overlay_cpu))
                    process.start()
                    send.close()
                    try:
                        limit = min(self.deadline, time.monotonic() + 0.30 + 2 * cap)
                        if not recv.poll(max(0, limit - time.monotonic())):
                            return plan
                        passed, imported, tests = recv.recv()
                    finally:
                        stop(process)
                certified = {self.profile.numbers[name] for name in passed} & candidates
                self.stats.update(patched_passed=len(certified), patched_overlay=overlay_cpu,
                                  patched_import=imported, patched_tests=tests)
                if certified:
                    return self.scheduler.repack(selected - certified, plan)
        except (EOFError, KeyError, OSError, SyntaxError, UnknownChange, ValueError):
            pass
        finally:
            spent = seconds() - begin
            self.remaining -= spent
            self.stats["patched_cpu"] = spent
            if "patched_passed" not in self.stats:
                self.stats["patched_incomplete"] = True
        return plan
