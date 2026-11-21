"""Bound replay of independent SQL assertions observed on the clean tree."""
import contextlib
import functools
import importlib
import os
import signal
import sys
import time

from assertion_index import AssertionIndex, ELIGIBLE, REPLAYABLE
from decision import PatchWorkspace
from impact import UnknownChange, patched, split_diff, syntax


def changed_objects(impact, text, trace):
    """Resolve touched base definitions and conservatively detect declarations."""
    touched = set()
    protected = 0
    whole_modules = False
    startup = False
    edits = split_diff(text)
    for path, hunks in edits.items():
        if not path.startswith("sqlglot/") or not path.endswith(".py") or path.endswith("/__init__.py"):
            return None
        original, (outer, scopes), _ = impact.base(path)
        modified = patched(original, hunks)
        _, (new_outer, revised), _ = syntax(path, modified)
        protected |= impact.files.get(path, {}).get(0, 0) | impact.details.flow.opaque
        if outer != new_outer or scopes.keys() != revised.keys():
            touched.update(trace.hits("m:" + path))
            whole_modules = True
            continue
        for key, (line, _, fingerprint, node) in scopes.items():
            if fingerprint == revised[key][2]:
                continue
            qual = ".".join(key[:-1])
            site = f"{path}:{line}:{qual}"
            touched.update(trace.hits("c:" + site))
            # A nested code object can be called after its outer method exits.
            # Source scope keys stop at the outer function. The build's scope
            # index also links Python's <locals> children to that definition.
            touched.update(trace.hits("s:" + site))
            startup |= bool(trace.hits("b:" + site))
    return touched, protected, whole_modules or startup


def tracked_value(name, original, paths, state):
    @functools.wraps(original)
    def observe(*args, **kwargs):
        if sys._getframe(1).f_code.co_filename.startswith(paths):
            state.add(name)
        return original(*args, **kwargs)
    return observe


def worker(send, root, index, tasks, cap):
    try:
        os.setsid()
        signal.signal(signal.SIGPROF, signal.SIG_DFL)
        signal.setitimer(signal.ITIMER_PROF, cap)
        with open(os.devnull, "w") as stream:
            os.dup2(stream.fileno(), 1)
            os.dup2(stream.fileno(), 2)
        os.chdir(root)
        sys.path.insert(0, str(root))
        import random
        import warnings

        prefix = str(root) + "/"
        paths = (prefix + "sqlglot/", prefix + "tests/")
        state = set()
        enabled = False

        def audit(event, args):
            if not enabled:
                return
            if event == "open":
                name = args[0]
                if not isinstance(name, (str, bytes)):
                    state.add("files")
                    return
                path = os.fsdecode(name)
                if not path.startswith(prefix + "sqlglot/") or not path.endswith((".py", ".pyc")):
                    state.add("files")
            elif event.startswith(("os.putenv", "os.unsetenv")):
                state.add("environment")

        sys.addaudithook(audit)
        for owner, attr, reason in ((os, "getenv", "environment"),
                                    (type(os.environ), "__getitem__", "environment"), (time, "time", "time"),
                                    (time, "monotonic", "time"), (random, "random", "randomness"),
                                    (random, "randint", "randomness"), (random, "choice", "randomness"),
                                    (os, "urandom", "randomness"), (warnings, "warn", "warnings")):
            setattr(owner, attr, tracked_value(reason, getattr(owner, attr), paths, state))

        for number, name, units in tasks:
            node, *parents, test = name.split("::")
            klass = importlib.import_module(node[:-3].replace("/", "."))
            for part in parents:
                klass = getattr(klass, part)
            instance = klass(test)
            try:
                for i in units:
                    kind, *payload = index.case(i)
                    state.clear()
                    enabled = True
                    try:
                        if kind == "helper":
                            name, args, kwargs = payload
                            getattr(instance, name)(*args, **kwargs)
                        elif kind == "direct":
                            recipe, values = payload
                            namespace = getattr(type(instance), test).__globals__
                            eval(recipe, namespace, {"self": instance, **values})
                        else:
                            raise ValueError("Unsupported assertion payload")
                    finally:
                        enabled = False
                    send.send(("replayed", 1))
                    if state:
                        send.send(("hazard", sorted(state)))
                        break
                else:
                    send.send(("pass", number))
            except Exception:
                # An exception retains this test. Later independent tests can
                # still complete within the same CPU reservation.
                send.send(("error", number))
    except (Exception, SystemExit):
        pass
    finally:
        send.close()


class AssertionReplay:
    def __init__(self, impact, scheduler, tests, filename, cap):
        import multiprocessing

        self.impact = impact
        self.scheduler = scheduler
        self.nodes = {row["number"]: row["id"] for row in tests}
        self.index = AssertionIndex(filename) if filename.is_file() else None
        self.ctx = multiprocessing.get_context("fork")
        self.cap = cap
        self.stats = {}

    def refine(self, text, plan, workspace=None):
        self.stats = {"candidate_units": 0, "replayed_units": 0, "dropped_tests": 0}
        if self.index is None or not plan:
            return plan
        try:
            footprint = changed_objects(self.impact, text, self.index)
            if footprint is None:
                return plan
            affected, protected, broad = footprint
            selected = {number for worker in plan for number in worker}
            self.stats["candidate_units"] = len(affected)
            if broad:
                self.stats["declaration"] = True
            passing = set()
            tasks = []
            for number in sorted(selected, key=lambda n: (-self.scheduler.durations[n], n)):
                bit = self.scheduler.bits[number]
                if not bit or bit & protected:
                    continue
                name = self.nodes[number]
                all_units = self.index.hits("t:" + name)
                if not all_units or not self.index.flags(all_units[0]) & ELIGIBLE:
                    continue
                observed = tuple(unit for unit in all_units if unit in affected)
                if any(not self.index.flags(unit) & REPLAYABLE for unit in observed):
                    continue
                if broad:
                    units = tuple(unit for unit in all_units if self.index.flags(unit) & REPLAYABLE)
                    # The unchanged test harness can be replayed from the start.
                    # If any actual helper arguments are missing, keep the test.
                    payloads = [self.index.case(unit) for unit in all_units if unit not in units]
                    if not units or any(case and case[0] not in ("subtest",) for case in payloads):
                        continue
                    self.stats["candidate_units"] += len(set(units) - affected)
                else:
                    units = observed
                if units:
                    tasks.append((number, name, units))
                else:
                    passing.add(number)
            plan = self.scheduler.repack(selected - passing, plan) if passing else plan
            self.stats["dropped_tests"] = len(passing)
            selected -= passing
            if not tasks or len(os.listdir("/proc/self/task")) != 1 or "sqlglot" in sys.modules:
                return plan
            hypothetical = self.scheduler.repack(selected - {task[0] for task in tasks}, plan)
            saving = (self.scheduler.cost(plan) - self.scheduler.cost(hypothetical)) / 1000
            cap = self.cap(min(2.5, 0.65 * saving))
            if cap < 0.23:
                return plan
            self.stats["attempted_candidates"] = sum(len(task[2]) for task in tasks)
            with contextlib.nullcontext(workspace) if workspace else PatchWorkspace(self.impact.root, text) as overlay:
                passing = self.run(overlay.root(), tasks, cap)
            self.stats["dropped_tests"] += len(passing)
            return self.scheduler.repack(selected - passing, plan) if passing else plan
        except (OSError, SyntaxError, UnknownChange, UnicodeError, ValueError, RecursionError, KeyError):
            return plan

    def run(self, root, tasks, cap):
        passing = set()
        with contextlib.ExitStack() as stack:
            recv, send = self.ctx.Pipe(duplex=False)
            stack.enter_context(recv)
            stack.enter_context(send)
            child = self.ctx.Process(target=worker, args=(send, root, self.index, tasks, cap))
            end = time.monotonic() + 1.7 * cap + 0.1
            child.start()
            send.close()
            try:
                while recv.poll(max(0, end - time.monotonic())):
                    outcome, value = recv.recv()
                    if outcome == "pass":
                        passing.add(value)
                    elif outcome == "replayed":
                        self.stats["replayed_units"] += value
                    elif outcome == "hazard":
                        self.stats.setdefault("hazards", []).extend(value)
            except EOFError:
                pass
            finally:
                child.join(0.01)
                if child.is_alive():
                    with contextlib.suppress(ProcessLookupError):
                        if os.getpgid(child.pid) == child.pid:
                            os.killpg(child.pid, signal.SIGKILL)
                        else:
                            child.kill()
                    child.join()
        return passing
