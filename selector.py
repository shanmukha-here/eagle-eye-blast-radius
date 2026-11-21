"""Schedule numbered tests using the impact map of the pinned source tree."""
import json
import math
import os
import sys
import time
import zlib
from functools import lru_cache
from pathlib import Path

from incumbent.impact import Impact as IncumbentImpact
from incumbent.refine import Details as IncumbentDetails
from incumbent.optimizer_probe import Replay as IncumbentReplay

from cpu_budget import CpuBudget, seconds
from cold_import import ColdImport
from dependencies import Dependencies
from module_topology import addition, hard_topology, opaque_format
from decision import Execution, PatchWorkspace
from flow import Flow
from observed import Observers
from exact_tests import ExactTests
from optimizer_probe import Replay
from miniatures import CORE, Miniatures
from startup import Startup
from narrow import Narrow
from observation_index import ObservationIndex
from surface import Surface
from parser_signal import unexecuted_return
from reference_replay import ReferenceReplay
from long_gate import LongGate
from long_cases import LongCases
from tpcds_replay import QueryGate
from receiver_edits import ReceiverEdits
from registry_guard import changes_routing
from precision import Precision
from recovery import Recovery
from risk_policy import RiskPolicy
from impact import Impact, UnknownChange, bypasses_executor_result, patched, split_diff, syntax
from refine import Details


class Scheduler:
    def __init__(self, tests, profile_ids):
        self.durations = {t["number"]: math.floor(t["seconds"] * 1000 + 0.5) for t in tests}
        self.order = sorted(self.durations, key=lambda n: (-self.durations[n], n))
        bits = {name: 1 << i for i, name in enumerate(profile_ids)}
        self.bits = {t["number"]: bits.get(t["id"], 0) for t in tests}
        self.known = 0
        for bit in self.bits.values():
            self.known |= bit
        self.unmapped = any(not bit for bit in self.bits.values())
        self.insurance = tuple(n for n in reversed(self.order)
                               if self.bits[n] and self.durations[n] <= 1)

    @lru_cache(maxsize=384)
    def packed(self, mask, pad=True):
        # Unknown test IDs are always included.
        selected = [n for n in self.order if not self.bits[n] or self.bits[n] & mask]
        if not selected:
            return ()
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
            return tuple(map(tuple, best_bins))
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
        return tuple(map(tuple, best_bins))

    def schedule(self, mask, pad=True):
        return json.dumps(self.packed(mask, pad), separators=(",", ":"))

    def plan(self, mask):
        return [list(group) for group in self.packed(mask, False)]

    def cost(self, workers):
        return 2000 * len(workers) + max(
            (sum(self.durations[n] for n in worker) for worker in workers), default=0
        )

    def insure(self, workers, nearby, passed):
        """Place inexpensive related tests in existing slack."""
        if not nearby or not workers:
            return workers
        loads = [sum(self.durations[n] for n in group) for group in workers]
        limit = max(loads)
        selected = {n for group in workers for n in group}
        groups = [group.copy() for group in workers]
        added = 0
        for n in self.insurance:
            if n in selected or n in passed or not nearby & self.bits[n]:
                continue
            seat = loads.index(min(loads))
            new_load = loads[seat] + self.durations[n]
            if new_load > limit:
                break
            groups[seat].append(n)
            loads[seat] = new_load
            added += 1
            if added == 64:
                break
        return groups if added else workers

    def intersect(self, before, after, keep=0):
        old = self.packed(before)
        new = self.packed(after)
        old_ids = {n for group in old for n in group}
        new_ids = {n for group in new for n in group}
        selected = (old_ids & new_ids) | {n for n in new_ids if self.bits[n] & keep}
        return self.repack(selected, old, new)

    def replay_matters(self, old, reduced, current, keep):
        a = self.intersect(old, current, keep)
        b = self.intersect(reduced, current, keep)
        return self.cost(a) != self.cost(b) or {
            n for group in a for n in group
        } != {n for group in b for n in group}

    def repack(self, selected, *previous):
        mask = 0
        for n in selected:
            mask |= self.bits[n]
        packed = self.packed(mask, False)
        variants = [packed]
        for groups in previous:
            filtered = [[n for n in group if n in selected] for group in groups]
            filtered = [group for group in filtered if group]
            if len({n for group in filtered for n in group}) == len(selected):
                variants.append(filtered)
        best = min(variants, key=lambda groups: (self.cost(groups), len(groups)))
        return [list(group) for group in best] if best is packed else best


def needs_parser_guard(impact, text):
    if not any(f"diff --git a/{path} " in text for path in CORE) and "diff --git a/sqlglot/executor/" not in text:
        return False
    try:
        for path, hunks in split_diff(text).items():
            if path not in CORE and not path.startswith("sqlglot/executor/"):
                continue
            source, (outer, scopes), _ = impact.base(path)
            _, (new_outer, changed), _ = syntax(path, patched(source, hunks))
            if outer != new_outer or scopes.keys() != changed.keys():
                if path in CORE:
                    return True
                continue
            for key in scopes:
                if scopes[key][2] == changed[key][2]:
                    continue
                if path in CORE and len(key) > 2:
                    return True
                if path not in CORE and bypasses_executor_result(scopes[key][3], changed[key][3]):
                    return True
    except (UnknownChange, OSError, SyntaxError, UnicodeError, ValueError, RecursionError):
        return True
    return False



def main(data=Path("/data"), base=Path("/base"), map_path=Path(__file__).with_name("base.z")):
    started = seconds()
    tests = json.loads((data / "tests.json").read_bytes())["tests"]
    profile = json.loads(zlib.decompress(map_path.read_bytes()))
    data_map = json.loads(zlib.decompress(map_path.with_name("details.z").read_bytes()))
    access = Dependencies(json.loads(zlib.decompress(map_path.with_name("access.z").read_bytes())))
    flow_map = json.loads(zlib.decompress(map_path.with_name("flow.z").read_bytes()))
    impact = Impact(base, profile, Details(data_map, profile["ids"], base, access))
    cold_file = map_path.with_name("cold_import.index")
    if cold_file.is_file():
        impact.details.cold_import = ColdImport(cold_file, impact)
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
    incumbent.details.replay.query_lines = (impact.details.replay.line_ids, impact.details.replay.line_pool)
    incumbent.details.replay.covered_lines = impact.details.lines
    incumbent.scheduler = scheduler
    incumbent.base = impact.base
    legacy_import = incumbent.details.import_valid

    def shared_import(path, source):
        return (legacy_import if path.endswith("/__init__.py") else impact.details.import_valid)(path, source)

    incumbent.details.import_valid = shared_import
    patches = sorted((data / "commits").glob("*.patch"))
    budget = CpuBudget(started, len(patches))
    narrow = Narrow(impact, map_path.parent)
    execution = Execution(scheduler, profile["ids"], base, len(patches), impact.details.lines)
    gate = LongGate(scheduler, profile["ids"], base, len(patches))
    queries = QueryGate(map_path.parent, impact, scheduler, tests)
    long_cases = LongCases(map_path.parent, impact, scheduler, tests, len(patches))
    receivers = ReceiverEdits(map_path.with_name("receiver.index"), impact)
    precision = Precision(map_path.parent, impact, scheduler, queries.test)
    risk = RiskPolicy(map_path.parent, impact, precision, scheduler, tests)
    recovery = Recovery(map_path.parent, impact, receivers)
    fallback_plan = scheduler.intersect(impact.full, impact.full)
    report = os.environ.get("BLAST_ASSERTION_REPORT") == "1"

    def insurance_scope(text):
        mask = 0
        try:
            for path in split_diff(text):
                mask |= (impact.visitors.get(path, 0) | impact.details.imports.get(path, 0) |
                         impact.details.dialects.get(path, 0) | impact.details.test_files.get(path, 0) |
                         impact.files.get(path, {}).get(0, 0))
        except UnknownChange:
            pass
        return mask

    def emit(path, plan, beginning, text=None, verified=False):
        plan = risk.apply(None if verified else text, plan, {**queries.stats, **precision.stats} if text else None)
        recovered = None if verified else recovery.declarations(text)
        if recovered is not None:
            mask, replace = recovered
            selected = {n for group in plan for n in group}
            extra = {n for n, bit in scheduler.bits.items() if not bit or bit & mask}
            # The import closure widens to cover definition side effects. A
            # long test introduced by that closure still needs a trace route
            # through at least one changed module.
            relevant = (extra if replace else selected | extra) & long_cases.tests.keys()
            retained = long_cases.recovery_scope(text, relevant) if relevant else relevant
            skipped = relevant - retained
            if skipped:
                precision.stats["recovery_long_unobserved"] = sorted(skipped)
            selected = (extra if replace else selected | extra) - skipped
            plan = scheduler.repack(selected, plan)
            plan = long_cases.recovery_replay(text, plan, base.resolve(), PatchWorkspace,
                                             retained & (extra - selected), deadline)
            precision.stats["import_recovery"] = mask.bit_count()
            precision.stats["recovery_replaces"] = replace
        if text:
            checked = verify.certify_small(text, plan, precision.short_certificate)
            if checked != plan:
                precision.stats["small_clean_cohort"] = sum(map(len, plan))
                plan = checked
            if plan:
                insured = scheduler.insure(plan, insurance_scope(text), verify.passed)
                if insured is not plan:
                    precision.stats["insurance_tests"] = sum(map(len, insured)) - sum(map(len, plan))
                    plan = insured
        if report:
            metrics = dict(commit=path.stem, cpu=seconds() - beginning,
                           startup=2 * len(plan),
                           busiest=(scheduler.cost(plan) - 2000 * len(plan)) / 1000,
                           **queries.stats, **precision.stats, **long_cases.stats, **risk.stats)
            print(json.dumps(metrics, separators=(",", ":")), file=sys.stderr)
        workers = json.dumps(plan, separators=(",", ":"))
        sys.stdout.write('{"commit":' + json.dumps(path.stem) + ',"workers":' + workers + '}\n')
    observation = ObservationIndex(impact, map_path.with_name("values.z"))
    surface = Surface(impact, map_path.parent, observation.references)
    references = ReferenceReplay(base, map_path.parent, scheduler, tests, len(patches))
    verify = ExactTests(scheduler, tests, base, len(patches), map_path.with_name("unit_ready.json"))
    for check in (impact.details.replay,
                  incumbent.details.replay, impact.details.miniatures,
                  execution, gate, references, verify):
        check.cpu_limit = budget.cap
    parser_bit = impact.details.miniatures.tests.get("parser", 0)
    parser_id = next((number for number, bit in scheduler.bits.items() if bit and bit == parser_bit), None)
    deadline = time.monotonic() + min(150, 2.5 * len(patches) + 5)
    for index, path in enumerate(patches):
        budget.remaining_patches = len(patches) - index
        beginning = seconds() if report else 0
        risk_text = None
        queries.stats = {}
        precision.stats = {}
        long_cases.stats = {}
        verify.attempted.clear()
        verify.passed.clear()
        before = mask = impact.full
        try:
            if time.monotonic() >= deadline or path.stat().st_size > 1_000_000:
                plan = fallback_plan
            else:
                text = path.read_bytes().decode("utf-8")
                if hard_topology(text) or opaque_format(text):
                    emit(path, fallback_plan, beginning)
                    continue
                if "NUMERIC_ESCAPES" in text:
                    observed = precision.registry(text)
                    if observed is not None:
                        plan = scheduler.plan(observed)
                        emit(path, plan, beginning, verified=True)
                        continue
                if changes_routing(impact, text):
                    observed = precision.registry(text)
                    plan = (fallback_plan if observed is None else
                            scheduler.plan(observed))
                    emit(path, plan, beginning)
                    continue
                metadata = surface.select(text)
                if addition(text) and metadata is None:
                    emit(path, fallback_plan, beginning)
                    continue
                risk_text = text
                recovered = recovery.declarations(text) if metadata is None else None
                if recovered is not None and recovered[1]:
                    mask = recovered[0]
                    plan = scheduler.plan(mask)
                    with PatchWorkspace(base.resolve(), text) as workspace:
                        plan = long_cases.apply(text, plan, workspace, budget.cap, declaration=True)
                        if plan and all(scheduler.durations[n] < 3000 for w in plan for n in w):
                            checked = verify.refine(text, plan, workspace=workspace)
                            # Definition-time behavior can change even when
                            # the candidate methods pass their assertions.
                            if checked:
                                plan = checked
                    precision.stats["import_recovery"] = mask.bit_count()
                    precision.stats["recovery_replaces"] = True
                    emit(path, plan, beginning, risk_text, verified=True)
                    continue
                direct = observation.edit(text) if metadata is None else ("metadata", metadata)
                local = precision.local_plan(text) if not direct else None
                if local is not None:
                    plan = scheduler.plan(local)
                    with PatchWorkspace(base.resolve(), text) as workspace:
                        plan = long_cases.apply(text, plan, workspace, budget.cap)
                        if plan and precision.short_certificate(text):
                            cpu = seconds() if report else 0.0
                            before_count = sum(map(len, plan))
                            plan = verify.refine(text, plan, workspace=workspace, short_only=True)
                            if not plan:
                                precision.stats["short_certified"] = before_count
                            if report:
                                precision.stats["short_cpu"] = round(seconds() - cpu, 6)
                    emit(path, plan, beginning, risk_text, verified=True)
                    continue
                if direct is not None:
                    kind, observed = direct
                    observed_plan = scheduler.plan(observed)
                    if kind == "syntax":
                        observed_plan = verify.collection(text, observed_plan)
                    if observed_plan is not None and (
                        kind in ("default", "attribute", "import", "metadata", "syntax") or scheduler.cost(observed_plan) <= 3000
                    ):
                        plan = references.apply(text, observed_plan)
                        plan = verify.refine(text, plan)
                        emit(path, plan, beginning, risk_text, verified=kind in ("metadata", "fixture"))
                        continue
                with PatchWorkspace(base.resolve(), text) as workspace:
                    mask = execution.refine(text, narrow.select(text, impact.select(text)), workspace)
                    keep = 0
                    legacy = incumbent.details.replay
                    legacy.graph_changed = False
                    # An empty emitted plan is its own intersection.
                    if scheduler.unmapped or mask & scheduler.known:
                        parser = impact.details.miniatures.tests.get("parser", 0)
                        if mask & parser and needs_parser_guard(impact, text):
                            keep = parser
                        incumbent.details.replay.relevant = lambda old, reduced: scheduler.replay_matters(
                            old, reduced, mask, keep
                        )
                        before = incumbent.select(text)
                    plan = scheduler.intersect(before, mask, keep)
                    if before == mask == impact.full:
                        declaration = surface.select(text, fast=False)
                        if declaration is None:
                            declaration = receivers.select(text)
                            if declaration is not None:
                                queries.stats["receiver_declaration"] = True
                        if declaration is not None:
                            before = mask = declaration | keep
                            plan = scheduler.plan(mask)
                    # Padding that did not change the initial schedule cost is
                    # not an observation. Replaying those IDs can load unrelated
                    # class fixtures and make a cheap cohort expensive.
                    observed = (before & mask) | keep
                    active = {n for group in plan for n in group
                              if not scheduler.bits[n] or scheduler.bits[n] & observed}
                    replay_plan, query_mask = precision.body(text, scheduler.repack(active, plan))
                    replay_plan = long_cases.apply(text, replay_plan, workspace, budget.cap)
                    plan = references.apply(text, replay_plan)
                    if not legacy.graph_changed:
                        plan = queries.apply(text, plan, workspace, budget.cap, query_mask)
                    plan = gate.apply(text, plan, workspace)
                    if parser_id is not None and any(parser_id in group for group in plan):
                        if unexecuted_return(impact, text, parser_bit):
                            retained = {n for group in plan for n in group if n != parser_id}
                            plan = scheduler.repack(retained, plan)
                    short_only = bool(plan) and all(scheduler.durations[n] < 3000 for group in plan for n in group)
                    if short_only and precision.short_certificate(text):
                        cpu = seconds() if report else 0.0
                        checked = verify.refine(text, plan, workspace=workspace, short_only=True)
                        if not checked:
                            precision.stats["short_certified"] = sum(map(len, plan))
                        if report:
                            precision.stats["short_cpu"] = round(seconds() - cpu, 6)
                        plan = checked
                    else:
                        plan = verify.refine(text, plan, workspace=workspace)
        except UnicodeError:
            plan = fallback_plan
        emit(path, plan, beginning, risk_text)
    if report and cold_file.is_file():
        print(json.dumps(dict(cold_import_checks_skipped=impact.details.cold_import.shortcuts)), file=sys.stderr)


if __name__ == "__main__":
    main()
