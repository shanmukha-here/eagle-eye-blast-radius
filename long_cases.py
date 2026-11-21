"""Replay independent inputs from the five expensive tests on the patched tree."""

import ast
import contextlib
from hashlib import sha256
import importlib
import inspect
import multiprocessing
import os
from pathlib import Path
import pickle
import signal
from struct import Struct
import sys
import time

from cpu_budget import seconds
from impact import patched, split_diff, syntax, UnknownChange
from narrow import imports
from precision_body import footprint
from precision_index import PrecisionIndex
from short_probe import memory


METHODS = (
    "test_max_nodes", "test_generator_memory", "test_execute_tpch",
    "test_optimized_tpch", "test_execute_tpcds",
)
PARSER, MEMORY, EXECUTE_H, OPTIMIZED_H, EXECUTE_DS = range(len(METHODS))
MICROS = Struct("<I")
OPTIMIZED_SQL = 64  # Two SHA-256 digests.
FIXTURES = {
    f"tests/fixtures/optimizer/{family}/{family}.sql": tpch
    for family, tpch in (("tpc-h", True), ("tpc-ds", False))
}


def fixture_pairs(text):
    """Read the same fixture records used by the pinned tests."""
    records = "\n".join(line for line in text.splitlines() if line and not line.startswith("--")).split(";")
    pairs = []
    for index in range(0, len(records) - 1, 2):
        lines = records[index].strip().split("\n")
        meta = {}
        while lines and lines[0].startswith("#"):
            key, value = lines.pop(0).split(":", 1)
            meta[key.lstrip("#").strip()] = value.strip()
        if not lines:
            raise ValueError("Fixture record has no query")
        pairs.append((meta, "\n".join(lines), records[index + 1].strip()))
    return pairs


def parser_cases():
    """Reuse the pinned assertions, retaining the assignments each stage needs."""
    from tests.test_parser import TestParser

    source = inspect.getsource(TestParser.test_max_nodes)
    from textwrap import dedent

    method = ast.parse(dedent(source)).body[0]
    statements = method.body
    setup = next(i for i, statement in enumerate(statements)
                 if isinstance(statement, ast.Assign) and statement.targets[0].id == "width")
    checks = [i for i in range(setup, len(statements)) if isinstance(statements[i], ast.With)]
    if len(checks) != 3 or checks[1] != checks[0] + 2 or checks[2] != checks[1] + 2:
        raise ValueError("Unexpected pinned parser test")
    groups = (statements[:setup],
              statements[setup:checks[0] + 2],
              statements[setup:checks[0]] + statements[checks[1]:checks[1] + 2],
              statements[checks[2]:])
    scope = dict(inspect.getmodule(TestParser).__dict__)
    functions = []
    for body in groups:
        method.body = body
        tree = ast.Module(body=[method], type_ignores=[])
        exec(compile(tree, "<pinned parser-case>", "exec"), scope)
        functions.append(scope[method.name].__get__(TestParser(method.name)))
    return functions


class Inputs:
    """Keep the test fixtures and assertion functions together inside one fork."""

    def __init__(self):
        self.parser = self.dialects = self.executor = self.module = None
        self.fixtures = {}

    def prepare(self, method):
        if method == PARSER:
            if self.parser is None:
                self.parser = parser_cases()
            return len(self.parser)
        if method == MEMORY:
            if self.dialects is None:
                from sqlglot.dialects import Dialects

                self.dialects = tuple(Dialects)
            return len(self.dialects)
        key = method == EXECUTE_DS
        if key not in self.fixtures:
            from tests.helpers import load_sql_fixture_pairs

            family = "tpc-ds" if key else "tpc-h"
            self.fixtures[key] = tuple(load_sql_fixture_pairs(f"optimizer/{family}/{family}.sql"))
        return len(self.fixtures[key])

    def tables(self):
        if self.executor is None:
            self.module = importlib.import_module("tests.test_executor")
            self.executor = self.module.TestExecutor()
            self.executor.setUpClass()
        return self.executor, self.module

    def close(self):
        if self.executor is not None:
            self.executor.tearDownClass()

    def run(self, method, case, fingerprint=None):
        if method == PARSER:
            self.parser[case]()
            return None
        if method == MEMORY:
            if not memory((self.dialects[case],)):
                raise AssertionError("generator allocation grew")
            return None
        from sqlglot import parse_one, transpile

        if method == OPTIMIZED_H:
            _, source, optimized = self.fixtures[False][case]
            left = transpile(source, write="duckdb")[0]
            right = transpile(optimized, write="duckdb")[0]
            digest = sha256(left.encode()).digest() + sha256(right.encode()).digest()
            if digest != fingerprint:
                item, module = self.tables()
                a = item.tpch_conn.execute(left).fetchdf()
                b = item.tpch_conn.execute(right).fetchdf()
                item.rename_anonymous(b, a)
                module.assert_frame_equal(a, b)
            return digest

        tpch = method == EXECUTE_H
        args, sql, _ = self.fixtures[not tpch][case]
        # The original pool sends these objects through pickle before mp_execute.
        tree, data = pickle.loads(pickle.dumps((parse_one(sql), args)))
        if not data.get("execute"):
            return None
        item, module = self.tables()
        schema = module.TPCH_SCHEMA if tpch else module.TPCDS_SCHEMA
        tables = item.tpch_tables if tpch else item.tpcds_tables
        module.initializer(schema, tables)
        output = module.mp_execute(tree, data)
        if output is not None:
            item.subtestHelper(case, pickle.loads(pickle.dumps(output)), tpch=tpch)
        return None


def work(send, root, jobs, fingerprints, cap):
    try:
        os.setsid()
        signal.signal(signal.SIGPROF, signal.SIG_DFL)
        signal.setitimer(signal.ITIMER_PROF, cap)
        with open(os.devnull, "w") as sink:
            os.dup2(sink.fileno(), 1)
            os.dup2(sink.fileno(), 2)
        os.chdir(root)
        sys.path.insert(0, str(root))
        inputs = Inputs()
        # A setup or teardown failure invalidates all of this child's proofs.
        passed = []
        try:
            for test, method, count, cases in jobs:
                if inputs.prepare(method) != count:
                    continue
                try:
                    for case in cases:
                        start = case * OPTIMIZED_SQL
                        expected = fingerprints[start:start + OPTIMIZED_SQL] if method == OPTIMIZED_H else None
                        inputs.run(method, case, expected)
                except Exception:
                    continue
                passed.append(test)
        finally:
            inputs.close()
        send.send(passed)
    except (Exception, SystemExit):
        pass
    finally:
        send.close()


class LongCases:
    def __init__(self, directory, impact, scheduler, tests, count=0):
        self.impact = impact
        self.scheduler = scheduler
        self.tests = {}
        self.index = None
        for row in tests:
            name = row["id"].rsplit("::", 1)[-1]
            if name in METHODS and scheduler.durations[row["number"]] >= 3000:
                self.tests[row["number"]] = METHODS.index(name)
        path = directory / "long_cases.index"
        if path.is_file():
            self.index = PrecisionIndex(path)
            self.fingerprints = (directory / "long_cases.fingerprints").read_bytes()
        self.ctx = multiprocessing.get_context("fork")
        self.recovery_cpu = min(9.0, count * 0.15)
        self.stats = {}

    def changes(self, text, declaration=False):
        if self.index is None:
            return None
        fixture = self.fixture_changes(text)
        if fixture is not None:
            return fixture
        changes = footprint(self.impact, text)
        if changes is None:
            if not declaration or not self.index.mask("module_coverage"):
                return None
            edits = split_diff(text)
            if len(edits) != 1:
                return None
            path, = edits
            return [self.index.mask(f"{m}:{path}") for m in range(len(METHODS))]
        if len(changes) != 1:
            # Independent sub-case traces do not capture all interactions
            # between edits in separate modules and the suite's shared state.
            return None
        masks = [0] * len(METHODS)
        for path, sites in changes.items():
            for _, _, (body, anchors, control) in sites:
                # An insertion can be reached on a different flank of an
                # exception or control transfer than the recorded line.
                if anchors:
                    return None
                for start, end in body | control:
                    for line in range(start, end + 1):
                        for method in range(len(METHODS)):
                            masks[method] |= self.index.mask(f"{method}:{path}:{line}")
        return masks

    def fixture_changes(self, text):
        if not text.startswith("diff --git a/tests/fixtures/"):
            return None
        try:
            edits = split_diff(text)
            if len(edits) != 1:
                return None
            path, hunks = next(iter(edits.items()))
            if path not in FIXTURES:
                return None
            old = (self.impact.root / path).read_text()
            before, after = fixture_pairs(old), fixture_pairs(patched(old, hunks))
            method = EXECUTE_H if FIXTURES[path] else EXECUTE_DS
            count = self.index.mask(f"count:{method}")
            if not count or len(before) != len(after) or len(before) != count:
                return None
            masks = [0] * len(METHODS)
            for i, (a, b) in enumerate(zip(before, after)):
                if a[:2] != b[:2]:
                    masks[method] |= 2 << i
                if method == EXECUTE_H and a[1:] != b[1:]:
                    masks[OPTIMIZED_H] |= 2 << i
            return masks
        except (OSError, UnknownChange, UnicodeError, ValueError):
            return None

    def recovery_scope(self, text, active):
        """Keep long tests with a recorded route to the recovered module."""
        if self.index is None or not self.index.mask("module_coverage"):
            return active
        try:
            edits = split_diff(text)
        except UnknownChange:
            return active
        sources = []
        fixtures = set()
        observed = 0
        for path in edits:
            if path.startswith("tests/fixtures/"):
                readers = self.impact.files.get(path, {}).get(0)
                if readers is None:
                    return active
                observed |= readers
                if path in FIXTURES:
                    fixtures.update((EXECUTE_H, OPTIMIZED_H) if FIXTURES[path] else (EXECUTE_DS,))
                continue
            if not path.startswith("sqlglot/") or not path.endswith(".py") or path.endswith("/__init__.py"):
                return active
            sources.append(path)
            details = self.impact.details
            observed |= (details.imports.get(path, 0) | details.dialects.get(path, 0) |
                         self.impact.visitors.get(path, 0))
        return {test for test in active if observed & self.scheduler.bits[test]
                or self.tests[test] in fixtures
                or any(self.index.mask(f"{self.tests[test]}:{path}") for path in sources)}

    def apply(self, text, workers, workspace, cpu_limit, declaration=False, only=None):
        if self.index is None or "sqlglot" in sys.modules:
            return workers
        chosen = {n for worker in workers for n in worker}
        active = chosen & self.tests.keys()
        if only is not None:
            active &= only
        if not active:
            return workers
        masks = self.changes(text, declaration)
        if masks is None:
            return workers
        jobs = []
        cleared = set()
        expected = 0.22
        setup_changed = any(mask & 1 for mask in masks)
        for test in sorted(active, key=self.scheduler.durations.get, reverse=True):
            method = self.tests[test]
            count = self.index.mask(f"count:{method}")
            bits = masks[method]
            # The first bit describes common fixture setup or teardown.
            if not count or bits & 1:
                continue
            if not bits:
                if not setup_changed:
                    cleared.add(test)
                continue
            cases = tuple(i for i in range(count) if bits & (2 << i))
            cost = self.index.mask(f"setup:{method}") / 1e6
            cost += sum(self.index.mask(f"cpu:{method}:{i}") for i in cases) / 1e6
            if cost <= 0 or expected + cost > cpu_limit(4.0):
                continue
            expected += cost
            jobs.append((test, method, count, cases))
        revised = self.scheduler.repack(chosen - cleared, workers)
        if jobs:
            potential = self.scheduler.repack(chosen - cleared - {n for n, *_ in jobs}, revised)
            saving = (self.scheduler.cost(revised) - self.scheduler.cost(potential)) / 1000
            cap = cpu_limit(min(saving * 0.65, 1.35 * expected + 0.20, 4.0))
            if cap > expected:
                cleared.update(self.prove(workspace, jobs, cap))
                self.stats["long_cases_run"] = self.stats.get("long_cases_run", 0) + sum(
                    len(cases) for *_, cases in jobs
                )
        if cleared:
            self.stats["long_cases_cleared"] = sorted(
                cleared.union(self.stats.get("long_cases_cleared", ()))
            )
            return self.scheduler.repack(chosen - cleared, workers)
        return workers

    def recovery_replay(self, text, workers, base, workspace_factory, active, deadline):
        """Replay restored long tests with complete single-module evidence."""
        if not active or self.recovery_cpu < 0.3:
            return workers
        started = seconds()

        def allowance(requested):
            return max(0, min(requested, self.recovery_cpu - (seconds() - started),
                              (deadline - time.monotonic() - 0.30) / 1.60))

        try:
            edits = split_diff(text)
            if len(edits) != 1:
                return workers
            path, hunks = next(iter(edits.items()))
            if not path.startswith("sqlglot/") or not path.endswith(".py") or path.endswith("/__init__.py"):
                return workers
            source = self.impact.base(path)[0]
            before = syntax(path, source)[0]
            after = syntax(path, patched(source, hunks))[0]
            if imports(before) != imports(after):
                return workers
            cases = self.changes(text, declaration=True)
            if cases is None:
                return workers
            # A zero mask in a whole-module declaration trace is insufficient
            # to cover import-time and inheritance effects.
            active = {test for test in active if cases[self.tests[test]]}
            if active:
                with workspace_factory(base, text) as workspace:
                    return self.apply(text, workers, workspace, allowance, declaration=True, only=active)
        except (OSError, SyntaxError, UnicodeError, ValueError, UnknownChange, RecursionError):
            pass
        finally:
            cpu = seconds() - started
            self.recovery_cpu -= cpu
            self.stats["recovery_long_cpu"] = round(cpu, 6)
        return workers

    def prove(self, workspace, jobs, cap):
        try:
            root = workspace.root()
            with contextlib.ExitStack() as stack:
                recv, send = self.ctx.Pipe(duplex=False)
                stack.enter_context(recv)
                stack.enter_context(send)
                process = self.ctx.Process(target=work, args=(send, root, jobs, self.fingerprints, cap))
                process.start()
                send.close()
                try:
                    if recv.poll(1.55 * cap + 0.20):
                        return recv.recv()
                finally:
                    process.join(0.01)
                    if process.is_alive():
                        with contextlib.suppress(ProcessLookupError):
                            if os.getpgid(process.pid) == process.pid:
                                os.killpg(process.pid, signal.SIGKILL)
                            else:
                                process.kill()
                        process.join()
        except (EOFError, OSError, UnknownChange, SyntaxError, UnicodeError, ValueError):
            pass
        return ()
