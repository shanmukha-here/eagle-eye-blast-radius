"""Run covering tests against an isolated patched tree under a batch wall budget."""
import contextlib
import multiprocessing
import os
import re
import signal
import sys
import tempfile
import time
import unittest
from pathlib import Path

from impact import split_diff, patched, UnknownChange
from miniatures import CORE
from optimizer_probe import cost


def overlay(base, root, edits):
    """Only copy modified files; symlink untouched subtrees from the clean pin."""
    expanded = set()
    for name, hunks in edits.items():
        src = base / name
        if not src.is_file() or not name.startswith(("sqlglot/", "tests/")):
            raise ValueError("unsupported change")
        current = Path()
        for part in Path(name).parts[:-1]:
            if current not in expanded:
                for item in (base / current).iterdir():
                    if item.name != "__pycache__":
                        (root / current / item.name).symlink_to(item)
                expanded.add(current)
            current /= part
            dest = root / current
            if dest.is_symlink():
                dest.unlink()
                dest.mkdir()
        if current not in expanded:
            for item in (base / current).iterdir():
                if item.name != "__pycache__":
                    (root / current / item.name).symlink_to(item)
            expanded.add(current)
        dest = root / name
        dest.unlink()
        content = patched(src.read_text(), hunks)
        if name.endswith(".py"):
            compile(content, name, "exec", dont_inherit=True)
        dest.write_text(content)


def direct(ids):
    passed = []
    all_passed = True
    loader = unittest.TestLoader()
    for name in ids:
        mod, *parts = name.split("::")
        suite = loader.loadTestsFromName(".".join([mod[:-3].replace("/", "."), *parts]))
        result = unittest.TestResult()
        suite.run(result)
        if not (result.testsRun == 1 and result.wasSuccessful() and not result.skipped
                and not result.expectedFailures and not result.unexpectedSuccesses):
            all_passed = False
        else:
            passed.append(name)
    return passed, all_passed


def affected_dialects(paths):
    from sqlglot.dialects import Dialect
    modules = {path[:-3].replace("/", ".") for path in paths}
    return [name for name, dialect in Dialect.classes.items()
            if modules.intersection(c.__module__ for c in dialect.generator_class.__mro__)]


def worker(conn, root, inexpensive, checks, paths):
    try:
        os.setsid()
        with open(os.devnull, "w") as sink:
            os.dup2(sink.fileno(), 1)
            os.dup2(sink.fileno(), 2)
        sys.dont_write_bytecode = True
        sys.path.insert(0, str(root))
        os.chdir(root)
        cleared, good = direct(inexpensive)
        if good:
            for kind, node in checks:
                if kind == "memory":
                    from short_probe import memory
                    ok = memory(affected_dialects(paths))
                elif kind == "parser":
                    from parser_probe import exercise
                    exercise()
                    ok = True
                else:
                    from query_sentinel import check
                    ok = check()
                if ok:
                    cleared.append(node)
        conn.send(cleared)
    except BaseException:
        # The parent retains the entire incoming selection on an incomplete run.
        pass
    finally:
        conn.close()


def change_coverage(patch, lines):
    """Use removed lines on the pin and both neighbors of pure insertions."""
    cover = 0
    sensitive = False
    rows = {}
    old = None
    removed = False
    for text in patch.splitlines():
        if text.startswith("diff --git "):
            rows = lines.get(text.split()[2][2:], {})
            old = None
        elif text.startswith("@@ "):
            start, _, count = text.split()[1].partition(",")
            # For a zero-length old range, Git names the line *before* the insertion.
            old = -int(start) + (count == "0")
            removed = False
        elif old is not None:
            if text.startswith(" "):
                old += 1
                removed = False
            elif text.startswith("-"):
                cover |= rows.get(old, 0)
                old += 1
                removed = True
            elif text.startswith("+") and not removed:
                cover |= rows.get(old - 1, 0) | rows.get(old, 0)
            if text.startswith(("+", "-")) and re.search(
                r"max_nodes|_node_count|recurs|\blen\s*\(|\b[0-9][0-9_]{3,}\b", text
            ):
                sensitive = True
    return cover, sensitive


class Execution:
    def __init__(self, scheduler, names, base, count, lines):
        self.lines = lines
        self.scheduler = scheduler
        self.base = base.resolve()
        self.names = {n:i for i,n in enumerate(names)}
        self.durations = {name:scheduler.durations[num] for num,bit in scheduler.bits.items()
                          if bit for name in [names[bit.bit_length()-1]]}
        self.order = sorted(self.durations, key=self.durations.get, reverse=True)
        self.budget = min(45.0, count * 0.72)
        self.deadline = time.monotonic() + min(150.0, 2.4 * count + 3.0)
        self.ctx = multiprocessing.get_context("fork")
        # Both numerical libraries start single-threaded in the pinned image.
        # Their imports are shared; SQLGlot and test modules load only in a child.
        import pandas.testing
        import duckdb
        if "sqlglot" in sys.modules or len(os.listdir("/proc/self/task")) != 1:
            self.budget = 0.0

    def refine(self, patch, mask):
        if not mask or self.budget < 0.3 or time.monotonic() >= self.deadline:
            return mask
        try:
            edits = split_diff(patch)
        except (UnknownChange, ValueError):
            return mask
        if not edits or any(not p.startswith(("sqlglot/", "tests/fixtures/")) for p in edits):
            return mask
        original = [n for n in self.order if mask & (1 << self.names[n])]
        cheap = [n for n in original if self.durations[n] < 3000]
        cover, sensitive = change_coverage(patch, self.lines)
        covered = [n for n in cheap if cover & (1 << self.names[n])]
        total = sum(self.durations[n] for n in cheap)
        if total <= 550 and len(cheap) <= 40:
            inexpensive = covered
        else:
            # Favor the tests which can reduce the critical worker load. Expensive
            # integrations often exceed the decision-time budget just in setup.
            inexpensive = [n for n in covered if 15 <= self.durations[n] <= 230
                           and n.count("::") == 2 and "test_executor.py" not in n][:3]
        checks = []
        if len(edits) == 1:
            path = next(iter(edits))
            single = path.startswith("sqlglot/") and path.endswith(".py")
            for name in original:
                if name.endswith("::test_generator_memory") and path.startswith("sqlglot/generators/"):
                    checks.append(("memory", name))
                elif (name.endswith("::test_max_nodes") and single and path not in CORE and not sensitive
                      and not path.endswith("/__init__.py") and not path.startswith("sqlglot/executor/")
                      and path != "sqlglot/dialects/dialect.py"):
                    checks.append(("parser", name))
                elif (name.endswith("::test_execute_tpcds") and single and
                      path.startswith(("sqlglot/parsers/", "sqlglot/optimizer/"))
                      and not path.endswith("/__init__.py")):
                    checks.append(("tpcds", name))
        if checks and len(cheap) > 40 and inexpensive:
            # A genuinely cheap covering witness leaves time for the long workload.
            candidates = [n for n in reversed(covered) if n.count("::") == 2]
            inexpensive = candidates[:2]
        if not inexpensive and not checks:
            return mask
        requested = inexpensive + [name for _, name in checks]
        cheapest = mask
        for name in requested:
            cheapest &= ~(1 << self.names[name])
        gain = (cost(self.scheduler, mask) - cost(self.scheduler, cheapest)) / 1000
        if gain < 0.4:
            return mask
        # Repacking can leave the same long test on the critical worker even
        # after many cheap tests pass. Skip work whenever it cannot pay for itself.
        horizon = 6.0 if any(kind == "tpcds" for kind, _ in checks) else (2.5 if checks else 1.1)
        cap = min(self.budget, self.deadline-time.monotonic(), gain * 0.8, horizon)
        if cap < 0.3:
            return mask
        started = time.monotonic()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                overlay(self.base, root, edits)
                recv, send = self.ctx.Pipe(duplex=False)
                child = self.ctx.Process(target=worker, args=(send, root, inexpensive, checks, list(edits)))
                child.start()
                send.close()
                try:
                    if not recv.poll(max(0, cap-(time.monotonic()-started))):
                        return mask
                    cleared = recv.recv()
                    for name in cleared:
                        mask &= ~(1 << self.names[name])
                finally:
                    recv.close()
                    child.join(0.03)
                    if child.is_alive():
                        with contextlib.suppress(ProcessLookupError):
                            if os.getpgid(child.pid) == child.pid:
                                os.killpg(child.pid, signal.SIGKILL)
                            else:
                                child.kill()
                        child.join()
            return mask
        except (OSError, ValueError, EOFError, UnknownChange, SyntaxError, UnicodeError, RecursionError):
            return mask
        finally:
            self.budget -= time.monotonic()-started
