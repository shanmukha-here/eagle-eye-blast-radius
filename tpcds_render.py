"""Confirm TPC-DS reference SQL for edits isolated from execution."""

from hashlib import sha256
import multiprocessing
import os
import signal
import sys
import time

from cpu_budget import seconds
from impact import split_diff, UnknownChange
from precision_body import footprint
from precision_index import PrecisionIndex
from tpcds_replay import replay_in_child


FIXTURE = "optimizer/tpc-ds/tpc-ds.sql"


def fingerprint(text):
    return int.from_bytes(sha256(text.encode()).digest(), "little")


def render_worker(send, root, cases, count, cap):
    try:
        os.setsid()
        signal.signal(signal.SIGPROF, signal.SIG_DFL)
        signal.setitimer(signal.ITIMER_PROF, cap)
        with open(os.devnull, "w") as sink:
            os.dup2(sink.fileno(), 1)
            os.dup2(sink.fileno(), 2)
        os.chdir(root)
        sys.path.insert(0, str(root))
        from sqlglot import transpile
        from tests.helpers import load_sql_fixture_pairs

        rows = tuple(load_sql_fixture_pairs(FIXTURE))
        if len(rows) != count:
            return
        for number, raw, expected in cases:
            meta, sql, _ = rows[number]
            if not meta.get("execute") or fingerprint(sql) != raw:
                return
            rendered = transpile(sql, write="duckdb")
            if len(rendered) != 1 or fingerprint(rendered[0]) != expected:
                return
        send.send(True)
    except (Exception, SystemExit):
        pass
    finally:
        send.close()


class RenderProof:
    def __init__(self, directory, scheduler, test, count, impact, checked_cases):
        path = directory / "tpcds_render.index"
        self.index = PrecisionIndex(path) if path.is_file() else None
        self.scheduler = scheduler
        self.test = test
        self.impact = impact
        self.checked_cases = checked_cases
        self.context = multiprocessing.get_context("fork")
        self.available = min(12.0, count * 0.20)
        self.stats = {}

    def inputs(self, text):
        # Only a dialect generator may be isolated this way. Base generator
        # state and the shared executor are part of both sides of the test.
        source = self.checked_cases.sole_source(text, self.test)
        if source is None:
            return None
        path, section = source
        if not path.startswith("sqlglot/generators/"):
            return None
        if any(name.startswith("tests/fixtures/optimizer/tpc-") for name in split_diff(text)):
            return None
        read = self.index.mask
        if read("downstream:" + path):
            return None
        if read("loaded:" + path) and footprint(self.impact, section) is None:
            # A reference can observe class definitions without calling a
            # method in the defining module.
            return None
        bits = read("render:" + path)
        if not bits:
            return None
        cases = []
        while bits:
            bit = bits & -bits
            number = bit.bit_length() - 1
            raw = read(f"sql:{number}")
            expected = read(f"rendered:{number}")
            if not raw or not expected:
                return None
            cases.append((number, raw, expected))
            bits ^= bit
        return cases

    def apply(self, text, plan, workspace, deadline):
        if (self.index is None or self.available < 0.32 or "sqlglot" in sys.modules or
                not any(self.test in group for group in plan)):
            return plan
        if "diff --git a/sqlglot/generators/" not in text:
            return plan
        started = seconds()
        try:
            tests = {n for group in plan for n in group}
            reduced = self.scheduler.repack(tests - {self.test}, plan)
            gain = (self.scheduler.cost(plan) - self.scheduler.cost(reduced)) / 1000
            if gain < 0.70:
                return plan
            cases = self.inputs(text)
            if not cases:
                return plan
            expected = 0.24 + 0.016 * len(cases)
            cap = min(2.25, 1.65 * expected, 0.55 * gain, self.available - (seconds() - started),
                      (deadline - time.monotonic() - 0.20) / 1.6)
            if cap < expected:
                return plan
            self.stats["render_queries"] = len(cases)
            if replay_in_child(self.context, workspace, render_worker, (cases, self.index.mask("count")), cap):
                self.stats["render_cleared"] = True
                return reduced
        except (EOFError, OSError, SyntaxError, UnicodeError, UnknownChange, ValueError, RecursionError):
            pass
        finally:
            used = seconds() - started
            self.available -= used
            self.stats["render_cpu"] = round(used, 6)
        return plan
