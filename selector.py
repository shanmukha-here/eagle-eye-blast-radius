"""Schedule numbered tests using the impact map of the pinned source tree."""
import ast
import json
import math
import sys
import time
import zlib
from functools import lru_cache
from pathlib import Path

from incumbent.impact import Impact as IncumbentImpact
from incumbent.refine import Details as IncumbentDetails
from incumbent.optimizer_probe import Replay as IncumbentReplay

from dependencies import Dependencies
from decision import Execution
from flow import Flow
from observed import Observers
from optimizer_probe import Replay
from miniatures import CORE, Miniatures
from startup import Startup
from narrow import Narrow
from long_gate import LongGate
from impact import Impact, UnknownChange, patched, split_diff, structure
from refine import Details


class Scheduler:
    def __init__(self, tests, profile_ids):
        self.durations = {t["number"]: math.floor(t["seconds"] * 1000 + 0.5) for t in tests}
        self.order = sorted(self.durations, key=lambda n: (-self.durations[n], n))
        bits = {name: 1 << i for i, name in enumerate(profile_ids)}
        self.bits = {t["number"]: bits.get(t["id"], 0) for t in tests}

    @lru_cache(maxsize=384)
    def schedule(self, mask, pad=True):
        # Unknown test IDs are always included.
        selected = [n for n in self.order if not self.bits[n] or self.bits[n] & mask]
        if not selected:
            return "[]"
        best_cost = None
        best_bins = best_loads = None
        total = sum(self.durations[n] for n in selected)
        peak = self.durations[selected[0]]
        for k in range(1, min(len(selected), 8) + 1):
            lower = 2000 * k + max(peak, (total + k - 1) // k)
            if best_cost is not None and lower >= best_cost:
                continue
            loads, bins = [0] * k, [[] for _ in range(k)]
            for n in selected:
                j = loads.index(min(loads))
                loads[j] += self.durations[n]
                bins[j].append(n)
            bins = [b for b in bins if b]
            cost = max(loads) + 2000 * len(bins)
            if best_cost is None or cost < best_cost:
                best_bins, best_loads, best_cost = bins, loads[:len(bins)], cost
        if not pad:
            return json.dumps(best_bins, separators=(",", ":"))
        # Add extra checks in the idle time of already occupied workers.
        chosen = set(selected)
        limit = max(best_loads)
        for n in reversed(self.order):
            if n in chosen:
                continue
            j = best_loads.index(min(best_loads))
            duration = self.durations[n]
            if duration + best_loads[j] > limit:
                break
            best_bins[j].append(n)
            best_loads[j] += duration
        return json.dumps(best_bins, separators=(",", ":"))


    def cost(self, workers):
        return 2000 * len(workers) + max(
            (sum(self.durations[n] for n in worker) for worker in workers), default=0
        )

    def intersect(self, before, after, keep=0):
        old = json.loads(self.schedule(before))
        new = json.loads(self.schedule(after))
        old_ids = {n for group in old for n in group}
        new_ids = {n for group in new for n in group}
        selected = (old_ids & new_ids) | {n for n in new_ids if self.bits[n] & keep}
        return self.repack(selected, old, new)

    def repack(self, selected, *previous):
        mask = 0
        for n in selected:
            mask |= self.bits[n]
        packed = json.loads(self.schedule(mask, pad=False))
        variants = [packed]
        for groups in previous:
            filtered = [[n for n in group if n in selected] for group in groups]
            filtered = [group for group in filtered if group]
            if len({n for group in filtered for n in group}) == len(selected):
                variants.append(filtered)
        return min(variants, key=self.cost)


def changes_shared_grammar(impact, text):
    if not any(f"diff --git a/{path} " in text for path in CORE):
        return False
    try:
        for path, hunks in split_diff(text).items():
            if path not in CORE:
                continue
            source, (outer, scopes), _ = impact.base(path)
            new_outer, changed = structure(ast.parse(patched(source, hunks)))
            if outer != new_outer or scopes.keys() != changed.keys():
                return True
            if any(len(key) > 2 and scopes[key][2] != changed[key][2] for key in scopes):
                return True
    except (UnknownChange, OSError, SyntaxError, UnicodeError, ValueError, RecursionError):
        return True
    return False


def main(data=Path("/data"), base=Path("/base"), map_path=Path(__file__).with_name("base.z")):
    tests = json.loads((data / "tests.json").read_bytes())["tests"]
    profile = json.loads(zlib.decompress(map_path.read_bytes()))
    data_map = json.loads(zlib.decompress(map_path.with_name("details.z").read_bytes()))
    access = Dependencies(json.loads(zlib.decompress(map_path.with_name("access.z").read_bytes())))
    flow_map = json.loads(zlib.decompress(map_path.with_name("flow.z").read_bytes()))
    impact = Impact(base, profile, Details(data_map, profile["ids"], base, access))
    impact.details.flow = Flow(flow_map)
    impact.details.observers = Observers(json.loads(zlib.decompress(map_path.with_name("observers.z").read_bytes())))
    impact.details.flow.none_safe = impact.details.observers.none_safe
    scheduler = Scheduler(tests, profile["ids"])
    impact.details.replay = Replay(profile["ids"], base, map_path.parent)
    impact.details.startup = Startup(map_path.parent)
    impact.details.miniatures = Miniatures(profile["ids"], base, map_path.parent)
    impact.scheduler = scheduler
    incumbent = IncumbentImpact(base, profile, IncumbentDetails(data_map, profile["ids"], base, access))
    legacy = map_path.parent / "incumbent"
    incumbent.details.flow = Flow(flow_map)
    incumbent.details.observers = Observers(json.loads(zlib.decompress((legacy / "observers.z").read_bytes())))
    incumbent.details.flow.none_safe = incumbent.details.observers.none_safe
    incumbent.details.replay = IncumbentReplay(profile["ids"], base, legacy)
    incumbent.scheduler = scheduler
    patches = sorted((data / "commits").glob("*.patch"))
    narrow = Narrow(impact, map_path.parent)
    execution = Execution(scheduler, profile["ids"], base, len(patches), impact.details.lines)
    gate = LongGate(scheduler, profile["ids"], impact, len(patches))
    deadline = time.monotonic() + min(150, 2.5 * len(patches) + 5)
    for path in patches:
        before = mask = impact.full
        keep = 0
        text = None
        if time.monotonic() < deadline and path.stat().st_size <= 1_000_000:
            try:
                text = path.read_text("utf-8")
                before = incumbent.select(text)
                mask = execution.refine(text, narrow.select(text, impact.select(text)))
                # The incumbent lacks the full-width safeguard for core grammar
                # edits. Keep that safeguard when the newer selector requires it.
                parser = impact.details.miniatures.tests.get("parser", 0)
                if mask & ~before & parser and changes_shared_grammar(impact, text):
                    keep = parser
            except UnicodeError:
                pass
        plan = scheduler.intersect(before, mask, keep)
        if text is not None:
            plan = gate.apply(text, plan)
        workers = json.dumps(plan, separators=(",", ":"))
        sys.stdout.write('{"commit":' + json.dumps(path.stem) + ',"workers":' + workers + '}\n')


if __name__ == "__main__":
    main()
