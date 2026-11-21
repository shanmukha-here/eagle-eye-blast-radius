"""Check just the clean TPC-DS queries that can execute an edited body."""
import contextlib
import multiprocessing
import os
import signal
import sys
import time

from impact import UnknownChange, patched, split_diff, syntax
from observation_file import ObservationFile
from optimizer_probe import local_edit, verify_queries
from refine import bindings, regions


def query_worker(connection, root, store, checks, executor_only, cap):
    try:
        os.setsid()
        signal.signal(signal.SIGPROF, signal.SIG_DFL)
        signal.setitimer(signal.ITIMER_PROF, cap)
        with open(os.devnull, "w") as sink:
            os.dup2(sink.fileno(), 1)
            os.dup2(sink.fileno(), 2)
        os.chdir(root)
        sys.path.insert(0, str(root))
        if verify_queries(store, checks, executor_only):
            connection.send(True)
    except Exception:
        pass
    finally:
        connection.close()


class QueryGate:
    def __init__(self, directory, impact, scheduler, tests):
        filename = directory / "query_gate.index"
        self.index = ObservationFile(filename) if filename.is_file() else None
        self.store = directory / "optimizer_inputs"
        self.impact = impact
        self.scheduler = scheduler
        self.test = next((row["number"] for row in tests
                          if row["id"].endswith("::TestExecutor::test_execute_tpcds")), None)
        self.context = multiprocessing.get_context("fork")
        self.stats = {}

    def checks(self, text):
        edits = split_diff(text)
        if len(edits) != 1:
            return None
        path, hunks = next(iter(edits.items()))
        if (not path.startswith(("sqlglot/optimizer/", "sqlglot/executor/"))
                or path.endswith("/__init__.py") or self.index.get("boundary:" + path)):
            return None
        original, (outer, scopes), code = self.impact.base(path)
        _, (updated, revised), new_code = syntax(path, patched(original, hunks))
        if outer != updated or scopes.keys() != revised.keys() or bindings(code) != bindings(new_code):
            return None
        optimize = execute = 0
        found = False
        for key, (lineno, _, digest, before) in scopes.items():
            _, _, new_digest, after = revised[key]
            if digest == new_digest:
                continue
            if len(key) < 2 or key[-2].startswith(("__", "<")) or not local_edit(before, after):
                return None
            location = f"{path}:{lineno}:{'.'.join(key[:-1])}"
            hit = self.index.get("code:" + location)
            if not hit:
                return None
            changed = regions(before, after)
            if len(changed) == 1 and changed[0][0] == changed[0][1]:
                hit = self.index.get(f"line:{location}@{changed[0][0]}") or hit
            opt, full = hit[0][0]
            optimize |= opt
            execute |= full
            found = True
        if not found:
            return None
        checks = []
        relevant = optimize | execute
        for i in range(relevant.bit_length()):
            bit = 1 << i
            if not relevant & bit:
                continue
            row = self.index.get(f"case:{i}")
            if not row:
                return None
            checks.append((*row[0][0], bool(execute & bit)))
        checks.sort(key=lambda check: check[-1])
        return checks, not optimize and path.startswith("sqlglot/executor/")

    def apply(self, text, workers, workspace, cpu_limit):
        chosen = {n for group in workers for n in group}
        if self.index is None or self.test not in chosen or "sqlglot" in sys.modules:
            return workers
        reduced = self.scheduler.repack(chosen - {self.test}, workers)
        gain = (self.scheduler.cost(workers) - self.scheduler.cost(reduced)) / 1000
        cap = cpu_limit(min(gain * 0.55, 4.0))
        if cap < 0.35:
            return workers
        try:
            attempt = self.checks(text)
            if attempt is None:
                return workers
            checks, executor_only = attempt
            self.stats["tpcds_queries"] = len(checks)
            if not checks:
                self.stats["tpcds_cleared"] = True
                return reduced
            root = workspace.root()
            started = time.monotonic()
            with contextlib.ExitStack() as stack:
                recv, send = self.context.Pipe(duplex=False)
                stack.enter_context(recv)
                stack.enter_context(send)
                child = self.context.Process(target=query_worker,
                                             args=(send, root, self.store, checks, executor_only, cap))
                child.start()
                send.close()
                try:
                    if recv.poll(max(0.0, 1.6 * cap - (time.monotonic() - started))) and recv.recv() is True:
                        self.stats["tpcds_cleared"] = True
                        return reduced
                finally:
                    child.join(0.02)
                    if child.is_alive():
                        with contextlib.suppress(ProcessLookupError):
                            if os.getpgid(child.pid) == child.pid:
                                os.killpg(child.pid, signal.SIGKILL)
                            else:
                                child.kill()
                        child.join()
        except (EOFError, OSError, UnknownChange, SyntaxError, ValueError, RecursionError):
            pass
        return workers
