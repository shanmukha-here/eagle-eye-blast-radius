"""Certify individual short tests against a bounded patched checkout."""

import ast
from collections import defaultdict
from contextlib import suppress
from mmap import ACCESS_READ, mmap
import multiprocessing
import os
from pathlib import Path
import signal
import sys
import time

from cpu_budget import seconds
from decision import PatchWorkspace
from exact_tests import MICROSECONDS, run_unittests
from impact import UnknownChange, patched, split_diff, syntax
from warm_tests import WarmTests


PHASES = {"setup", "call", "teardown"}


class Outcomes:
    def __init__(self, names):
        self.names = set(names)
        self.phases = {}
        self.bad = set()
        self.bad_files = set()
        self.collection_error = False
        self.collected = 0.0

    def pytest_collection_modifyitems(self, config, items):
        selected = [item for item in items if item.nodeid in self.names]
        deselected = [item for item in items if item.nodeid not in self.names]
        items[:] = selected
        config.hook.pytest_deselected(items=deselected)

    def pytest_collection_finish(self):
        self.collected = seconds()

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


def execute(conn, root, names, cpu_cap, bare):
    try:
        start = seconds()
        os.setsid()
        signal.signal(signal.SIGPROF, signal.SIG_DFL)
        signal.setitimer(signal.ITIMER_PROF, cpu_cap)
        with open(os.devnull, "w") as sink:
            os.dup2(sink.fileno(), 1)
            os.dup2(sink.fileno(), 2)
        os.chdir(root)
        sys.path.insert(0, str(root))
        os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"

        # The parent warms only framework dependencies. Project and tests are
        # first imported here, with the patched checkout first on sys.path.
        import sqlglot
        if "sqlglot/__init__.py" != str(Path(sqlglot.__file__).relative_to(root)):
            return
        imported = seconds()
        if bare:
            passed = run_unittests(names, continue_after_failure=True)
            conn.send((passed, imported - start, 0.0, seconds() - imported))
            return

        import pytest

        result = Outcomes(names)
        paths = sorted({node.partition("::")[0] for node in names})
        status = pytest.main([
            "-q", "--tb=no", "-p", "no:cacheprovider", "-p", "pytest_timeout",
            "--timeout", "6", "--timeout-method", "signal",
            "--continue-on-collection-errors",
            "--rootdir", str(root), "--basetemp", str(root / "pytest-run"), *paths,
        ], plugins=[result])
        if status in (pytest.ExitCode.OK, pytest.ExitCode.TESTS_FAILED):
            conn.send((result.passed(), imported - start, result.collected - imported,
                       seconds() - result.collected))
    except (Exception, SystemExit):
        pass
    finally:
        conn.close()
        os._exit(0)


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
    def __init__(self, scheduler, profile, base, count, precision):
        self.scheduler = scheduler
        self.profile = profile
        self.base = base.resolve()
        self.context = multiprocessing.get_context("fork")
        self.grouped = None
        profile_path = Path(__file__).with_name("grouped_cpu.bin")
        if profile_path.is_file():
            with profile_path.open("rb") as file:
                self.grouped = mmap(file.fileno(), 0, access=ACCESS_READ)
        self.remaining = min(55.0, count * 1.15)
        self.deadline = time.monotonic() + (125 if count <= 40 else 185)
        self.available = len(os.listdir("/proc/self/task")) == 1
        self.precision = precision
        self.warm = WarmTests(self.base, profile.nodes.values())
        self.stats = {}

    def warm_shapes(self, edits, candidates):
        if any(not path.startswith("sqlglot/") or not path.endswith(".py") for path in edits):
            return None
        protected = self.precision.protected(edits)
        if any(self.scheduler.bits[n] & protected for n in candidates):
            return None
        changes = {}
        try:
            for path, hunks in edits.items():
                source, (outer, original), _ = self.precision.impact.base(path)
                _, (revised, after), _ = syntax(path, patched(source, hunks))
                if outer != revised or original.keys() != after.keys():
                    return None
                names = []
                for site, previous in original.items():
                    current = after[site]
                    if previous[2] == current[2]:
                        continue
                    if (not isinstance(previous[3], ast.Module)
                            or ast.get_docstring(previous[3], clean=False)
                            != ast.get_docstring(current[3], clean=False)):
                        return None
                    names.append(".".join(site[:-1]))
                if names:
                    changes[path] = tuple(names)
        except (OSError, SyntaxError, UnknownChange, ValueError, RecursionError):
            return None
        return changes

    def estimate(self, candidates, warm=False):
        floor = 0.0
        for n in candidates:
            position = (self.scheduler.bits[n].bit_length() - 1) * MICROSECONDS.size
            measured = 0
            if self.grouped is not None and 0 <= position <= len(self.grouped) - MICROSECONDS.size:
                measured, = MICROSECONDS.unpack_from(self.grouped, position)
            floor += measured * 1e-6 if measured else max(self.scheduler.durations[n], 2) * 0.00074
        fixtures = self.profile.fixture_cpu(candidates)
        return (0.06 if warm else 0.15) + max(floor, fixtures)

    def cohort(self, selected, candidates, plan, available):
        groups = defaultdict(set)
        for n in candidates:
            groups[self.profile.nodes[n].partition("::")[0]].add(n)
        choices = []
        for group in groups.values():
            expected = self.estimate(group)
            if expected > available:
                continue
            after = self.scheduler.repack(selected - group, plan)
            saving = (self.scheduler.cost(plan) - self.scheduler.cost(after)) / 1000
            if saving > 1.2 * expected:
                choices.append((saving / expected, min(group), group))
        if choices:
            return max(choices)[-1]
        return set()

    def refine(self, patch, plan, long_tests, known_passes, attempted):
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
        available = min(self.remaining, 2.5, upside * 0.90)
        expected = self.estimate(candidates)
        if expected > available:
            candidates = self.cohort(selected, candidates, plan, available)
            if not candidates:
                self.stats = {"patched_skipped": "cohort_cost"}
                return plan
            expected = self.estimate(candidates)
        cap = min(self.remaining, 2.9, upside, max(0.55, expected * 1.25 + 0.20))
        names = tuple(sorted(self.profile.nodes[n] for n in candidates))
        bare = all(name in self.profile.ready for name in names)
        self.stats = {"patched_candidates": len(names), "patched_unittest": bare}
        warm_cpu = 0.0
        try:
            # A new import for every child avoids aliased objects from the pin.
            # Importing pytest once amortizes framework startup across commits.
            if not bare:
                import pytest

            if any(name in sys.modules for name in ("sqlglot", "tests")):
                return plan
            with PatchWorkspace(self.base, patch) as workspace:
                root = workspace.root()
                overlay_cpu = seconds() - begin
                if cap - overlay_cpu < 0.15:
                    return plan
                changes = self.warm_shapes(edits, candidates) if bare else None
                if names in attempted and not changes:
                    return plan
                if changes and not self.warm.disabled:
                    result, cost, startup = self.warm.run(root, names, changes, cap - overlay_cpu, self.deadline)
                    warm_cpu = cost + startup
                    self.stats.update(patched_preload=startup, patched_warm_cpu=cost)
                    if result is not None:
                        passed, imported, tests = result
                        certified = {self.profile.numbers[name] for name in passed} & candidates
                        self.stats.update(patched_warm=True, patched_passed=len(certified),
                                          patched_overlay=overlay_cpu, patched_import=imported, patched_tests=tests)
                        known_passes.update(certified)
                        return self.scheduler.repack(selected - certified, plan) if certified else plan
                if names in attempted:
                    return plan
                recv, send = self.context.Pipe(duplex=False)
                with recv, send:
                    process = self.context.Process(target=execute, args=(send, root, names, cap - overlay_cpu, bare))
                    process.start()
                    send.close()
                    try:
                        limit = min(self.deadline, time.monotonic() + 0.30 + 2 * cap)
                        if not recv.poll(max(0, limit - time.monotonic())):
                            return plan
                        passed, imported, collected, tests = recv.recv()
                    finally:
                        stop(process)
                certified = {self.profile.numbers[name] for name in passed} & candidates
                self.stats.update(patched_passed=len(certified), patched_overlay=overlay_cpu,
                                  patched_import=imported, patched_collection=collected, patched_tests=tests)
                if certified:
                    known_passes.update(certified)
                    return self.scheduler.repack(selected - certified, plan)
        except (EOFError, KeyError, OSError, SyntaxError, UnknownChange, ValueError):
            pass
        finally:
            charged = seconds() - begin
            self.remaining -= charged + warm_cpu
            self.stats["patched_cpu"] = charged + warm_cpu
            self.stats["patched_parent_cpu"] = charged
            if "patched_passed" not in self.stats:
                self.stats["patched_incomplete"] = True
        return plan


def lane(conn, engine):
    """Run execution jobs alongside discovery on a second scorer core."""
    try:
        while True:
            message = conn.recv()
            if message is None:
                break
            text, plan, long_tests, passed, attempted = message
            plan = engine.refine(text, plan, long_tests, passed, attempted)
            conn.send((plan, passed, engine.stats))
    except (EOFError, OSError):
        pass
    finally:
        engine.warm.close()
        conn.close()


class ExecutionLane:
    def __init__(self, engine, long_tests):
        self.engine = engine
        self.long_tests = set(long_tests)
        self.process = self.conn = None
        self.started = 0.0
        self.available = engine.available

    def submit(self, text, plan, passed, attempted):
        if not self.available or not text or not plan or time.monotonic() >= self.engine.deadline:
            return False
        if self.process is None:
            self.conn, child_conn = self.engine.context.Pipe()
            self.process = self.engine.context.Process(target=lane, args=(child_conn, self.engine))
            try:
                self.process.start()
            except OSError:
                self.available = False
                self.process = None
                self.conn.close()
                self.conn = None
                return False
            finally:
                child_conn.close()
        try:
            self.started = time.monotonic()
            self.conn.send((text, plan, self.long_tests, passed, attempted))
            return True
        except OSError:
            self.available = False
            return False

    def finish(self, plan, passed):
        if self.available:
            try:
                deadline = min(self.engine.deadline, self.started + 12.0)
                if self.conn.poll(max(0.0, deadline - time.monotonic())):
                    return self.conn.recv()
            except (EOFError, OSError):
                pass
        self.available = False
        return plan, passed, {"patched_lane_incomplete": True}

    def close(self):
        if self.conn is not None:
            with suppress(OSError):
                self.conn.send(None)
            self.conn.close()
        if self.process is not None:
            self.process.join(0.20)
            if self.process.is_alive():
                self.process.kill()
                self.process.join()
