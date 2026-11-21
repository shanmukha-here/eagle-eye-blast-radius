"""Use small, patched workloads before retaining long integration tests."""
import contextlib
import multiprocessing
import os
import signal
import sys
import time

from decision import affected_dialects, PatchWorkspace
from impact import split_diff, UnknownChange
from miniatures import CORE


# Reduced parser runs did not preserve full-size failures. The intersection's
# original parser safeguards own that test.
LONG_TESTS = {
    "test_execute_tpcds": "tpcds",
    "test_generator_memory": "memory",
    "test_execute_tpch": "execute",
    "test_optimized_tpch": "optimized",
}


def probe(connection, root, targets, edits, first, reuse_file):
    os.setsid()
    with open(os.devnull, "w") as sink:
        os.dup2(sink.fileno(), 1)
        os.dup2(sink.fileno(), 2)
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(root))
    os.chdir(root)
    from memo_witness import same_cache_results
    from query_sentinel import check
    from short_probe import execute, memory, optimized

    for number, kind in targets:
        try:
            if kind == "memory":
                passed = memory(affected_dialects(edits))
            elif kind == "tpcds":
                if reuse_file == "memo":
                    passed = same_cache_results() or check(first, "sqlglot/schema.py")
                else:
                    passed = check(first, reuse_file)
            else:
                passed = (execute if kind == "execute" else optimized)()
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

    def supported(self, kind, edits):
        if kind == "tpcds":
            # SQL fingerprints do not check changes to the executor itself.
            return not any(path in CORE or path.startswith("sqlglot/executor/") or path == "sqlglot/serde.py"
                           for path in edits)
        if kind == "memory":
            return all(path.startswith(("sqlglot/generators/", "sqlglot/dialects/"))
                       or path == "sqlglot/generator.py" for path in edits)
        return not any(path.startswith("sqlglot/executor/") or path == "sqlglot/serde.py" for path in edits)

    def apply(self, text, workers, workspace=None, earlier=0.0, first=None, reuse=None,
              graph_changed=False):
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
            if self.supported(self.long[n], edits)
        ]
        if not targets:
            return workers
        smaller = self.scheduler.repack(run - {n for n, _ in targets}, workers)
        gain = (self.scheduler.cost(workers) - self.scheduler.cost(smaller)) / 1000
        cap = min(5.0, self.available, gain * 0.55)
        # The full TPC-DS witness must walk every executable query even on
        # an unchanged tree. Subsecond windows cannot certify that workload.
        if targets[0][1] == "tpcds" and earlier:
            # A changed optimizer graph requires complete result comparisons.
            # Do not retry that expensive witness after the screening replay.
            if graph_changed:
                return workers
            cap = min(cap, 4.0 - earlier)
        minimum = 1.35 if targets[0][1] == "tpcds" else 0.25
        if cap < minimum:
            return workers
        reuse_file = reuse() if reuse is not None and targets[0][1] == "tpcds" else None
        started = time.monotonic()
        try:
            with contextlib.nullcontext(workspace) if workspace else PatchWorkspace(self.base, text) as overlay_tree:
                root = overlay_tree.root()
                with contextlib.ExitStack() as stack:
                    recv, send = self.ctx.Pipe(duplex=False)
                    stack.enter_context(recv)
                    stack.enter_context(send)
                    child = self.ctx.Process(target=probe, args=(send, root, targets, list(edits), first, reuse_file))
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
