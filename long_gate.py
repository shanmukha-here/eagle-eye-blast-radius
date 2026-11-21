"""Use small, patched workloads before retaining long integration tests."""
import contextlib
import multiprocessing
import os
import signal
import sys
import tempfile
import time
from pathlib import Path

from decision import affected_dialects, overlay
from impact import split_diff, UnknownChange


LONG_TESTS = {
    "test_max_nodes": "parser",
    "test_execute_tpcds": "tpcds",
    "test_generator_memory": "memory",
    "test_execute_tpch": "execute",
    "test_optimized_tpch": "optimized",
}


def probe(connection, root, targets, edits):
    os.setsid()
    with open(os.devnull, "w") as sink:
        os.dup2(sink.fileno(), 1)
        os.dup2(sink.fileno(), 2)
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(root))
    os.chdir(root)
    from parser_probe import exercise
    from query_sentinel import check
    from short_probe import execute, memory, optimized

    for number, kind in targets:
        try:
            if kind == "parser":
                exercise()
                passed = True
            elif kind == "memory":
                passed = memory(affected_dialects(edits))
            else:
                passed = {"tpcds": check, "execute": execute, "optimized": optimized}[kind]()
            if passed:
                connection.send(number)
        except Exception:
            # A failed or incomplete witness keeps the original test selected.
            continue
    connection.close()


class LongGate:
    def __init__(self, scheduler, profile_ids, base, count):
        self.scheduler = scheduler
        self.base = base.resolve()
        self.ctx = multiprocessing.get_context("fork")
        self.available = min(70.0, count * 1.2)
        self.long = {}
        for number, bit in scheduler.bits.items():
            if not bit or scheduler.durations[number] < 3000:
                continue
            name = profile_ids[bit.bit_length() - 1].rsplit("::", 1)[-1]
            if name in LONG_TESTS:
                self.long[number] = LONG_TESTS[name]

    def apply(self, text, workers):
        if self.available < 0.25:
            return workers
        run = {n for group in workers for n in group}
        long = run & self.long.keys()
        if not long:
            return workers
        try:
            edits = split_diff(text)
        except UnknownChange:
            return workers
        if not edits or any(not name.startswith("sqlglot/") or not name.endswith(".py")
                            or name.endswith("/__init__.py") for name in edits):
            return workers

        targets = [
            (n, self.long[n]) for n in sorted(long, key=self.scheduler.durations.get, reverse=True)
            if self.long[n] != "memory" or all(path.startswith(("sqlglot/generators/", "sqlglot/dialects/"))
                                                or path == "sqlglot/generator.py" for path in edits)
        ]
        if not targets:
            return workers
        smaller = self.scheduler.repack(run - {n for n, _ in targets}, workers)
        gain = (self.scheduler.cost(workers) - self.scheduler.cost(smaller)) / 1000
        cap = min(5.0, self.available, gain * 0.65)
        if cap < 0.25:
            return workers
        started = time.monotonic()
        try:
            with tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                overlay(self.base, root, edits)
                with contextlib.ExitStack() as stack:
                    recv, send = self.ctx.Pipe(duplex=False)
                    stack.enter_context(recv)
                    stack.enter_context(send)
                    child = self.ctx.Process(target=probe, args=(send, root, targets, list(edits)))
                    child.start()
                    send.close()
                    try:
                        while recv.poll(max(0.0, cap - (time.monotonic() - started))):
                            run.discard(recv.recv())
                    except EOFError:
                        pass
                    finally:
                        child.join(0.02)
                        if child.is_alive():
                            with contextlib.suppress(ProcessLookupError):
                                if os.getpgid(child.pid) == child.pid:
                                    os.killpg(child.pid, signal.SIGKILL)
                                else:
                                    child.kill()
                            child.join()
        except (OSError, SyntaxError, UnknownChange, ValueError, RecursionError):
            return workers
        finally:
            self.available -= time.monotonic() - started
        return self.scheduler.repack(run, workers)
