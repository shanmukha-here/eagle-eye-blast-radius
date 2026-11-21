"""Check just the clean TPC-DS queries that can execute an edited body."""
import contextlib
import multiprocessing
import os
import signal
import sys
import time

from impact import UnknownChange, patched, split_diff, syntax
from cpu_budget import seconds
from observation_file import ObservationFile
from optimizer_probe import local_edit, verify_queries
from refine import bindings, regions


def estimated_cpu(checks):
    """Allow for a cold import and a complete result check on affected stages."""
    complete = sum(whole or not checkpoint for _, _, checkpoint, whole in checks)
    return 0.60 + 0.035 * (len(checks) - complete) + 0.254 * complete


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


def replay_in_child(context, workspace, target, args, cap):
    root = workspace.root()
    started = time.monotonic()
    with contextlib.ExitStack() as stack:
        recv, send = context.Pipe(duplex=False)
        stack.enter_context(recv)
        stack.enter_context(send)
        child = context.Process(target=target, args=(send, root, *args, cap))
        child.start()
        send.close()
        try:
            wait = max(0.0, 1.60 * cap - (time.monotonic() - started))
            return recv.poll(wait) and recv.recv() is True
        finally:
            child.join(0.02)
            if child.is_alive():
                with contextlib.suppress(ProcessLookupError):
                    if os.getpgid(child.pid) == child.pid:
                        os.killpg(child.pid, signal.SIGKILL)
                    else:
                        child.kill()
                child.join()


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
        self.recovery_cpu = 0.0
        self.stats = {}

    def checks(self, text, observed=None):
        edits = split_diff(text)
        if len(edits) != 1:
            return None
        path, hunks = next(iter(edits.items()))
        prior_scope = (path.startswith(("sqlglot/optimizer/", "sqlglot/executor/"))
                       or path in ("sqlglot/planner.py", "sqlglot/schema.py"))
        if (not path.startswith("sqlglot/") or path.endswith("/__init__.py")
                or self.index.get("boundary:" + path) or not prior_scope and observed is None):
            return None
        original, (outer, scopes), code = self.impact.base(path)
        _, (updated, revised), new_code = syntax(path, patched(original, hunks))
        if outer != updated or scopes.keys() != revised.keys() or bindings(code) != bindings(new_code):
            return None
        optimize = execute = 0
        found = False
        missing_stage = False
        for key, (lineno, _, digest, before) in scopes.items():
            _, _, new_digest, after = revised[key]
            if digest == new_digest:
                continue
            if len(key) < 2 or key[-2].startswith(("__", "<")) or not local_edit(before, after):
                return None
            location = f"{path}:{lineno}:{'.'.join(key[:-1])}"
            hit = self.index.get("code:" + location)
            if not hit:
                if observed is None:
                    return None
                missing_stage = True
                found = True
                continue
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
        unresolved = 0
        if observed is not None:
            # A query that reached the line in the independent full trace
            # needs a complete result check if its stage is unresolved here.
            unresolved = observed if missing_stage else observed & ~relevant
            execute |= unresolved
            relevant = observed
        for i in range(relevant.bit_length()):
            bit = 1 << i
            if not relevant & bit:
                continue
            row = self.index.get(f"case:{i}")
            if not row:
                return None
            checks.append((*row[0][0], bool(execute & bit)))
        checks.sort(key=lambda check: check[-1])
        return checks, not (optimize or unresolved) and path.startswith("sqlglot/executor/")

    def apply(self, text, workers, workspace, cpu_limit, observed=None, projected=None):
        chosen = {n for group in workers for n in group}
        if self.index is None or self.test not in chosen or "sqlglot" in sys.modules:
            return workers
        reduced = self.scheduler.repack(chosen - {self.test}, workers)
        gain = (self.scheduler.cost(workers) - self.scheduler.cost(reduced)) / 1000
        cap = cpu_limit(min(gain * 0.55, 4.0))
        if cap < 0.35:
            return workers
        try:
            attempt = projected if projected is not None else self.checks(text, observed)
            if attempt is None:
                return workers
            checks, executor_only = attempt
            if not checks:
                self.stats["tpcds_cleared"] = True
                return reduced
            if estimated_cpu(checks) > cap:
                self.stats["tpcds_over_budget"] = len(checks)
                return workers
            self.stats["tpcds_queries"] = len(checks)
            if replay_in_child(self.context, workspace, query_worker, (self.store, checks, executor_only), cap):
                self.stats["tpcds_cleared"] = True
                return reduced
        except (EOFError, OSError, UnknownChange, SyntaxError, ValueError, RecursionError):
            pass
        return workers

    def recovery(self, text, workers, long_cases, workspace, deadline):
        """Check late TPC-DS candidates against the complete patched tree."""
        if (self.index is None or self.recovery_cpu < 0.65 or
                not any(self.test in group for group in workers)):
            return workers
        started = seconds()

        def available(requested):
            return max(0, min(requested, self.recovery_cpu - (seconds() - started),
                              (deadline - time.monotonic() - 0.20) / 1.60))

        try:
            source = long_cases.sole_source(text, self.test)
            if source is None:
                return workers
            _, section = source
            proof = self.checks(section)
            if proof is None or not proof[0]:
                return workers
            new = self.apply(section, workers, workspace, available, projected=proof)
            if new is not workers:
                self.stats["recovery_tpcds_cleared"] = True
            return new
        except (OSError, SyntaxError, UnicodeError, UnknownChange, ValueError, RecursionError):
            return workers
        finally:
            used = seconds() - started
            self.recovery_cpu -= used
            self.stats["recovery_query_cpu"] = round(used, 6)
