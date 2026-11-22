"""Exact candidates for edits that only swap function code on the pinned import.

A test that never started a changed code object on the pin runs the same instructions on the
patched source, so it passes as it did on the pin. The map lists, for each code object, the
tests that started it in a fork of the fully imported suite. Code that already ran while
importing is excluded, because its effects persist without re-running it.
"""

import lzma
import marshal
import os
import time
from pathlib import Path

import zpatch
from impact import UnknownChange, patched, split_diff


class Candidates:
    def __init__(self, map_path, base, ids):
        self.base = Path(base)
        self.ready = False
        try:
            data = marshal.loads(lzma.decompress(Path(map_path).read_bytes()))
        except (OSError, ValueError, EOFError, lzma.LZMAError):
            return
        if data["ids"] != list(ids):
            return
        self.ids = data["ids"]
        self.base_map = map_path
        self.masks = dict(zip(map(tuple, data["keys"]), data["masks"]))
        self.preload = set(map(tuple, data["preload"]))
        # Code that runs differently after earlier tests changed shared state.
        self.preload.update(map(tuple, data.get("sensitive", ())))
        self.opens = data["opens"]
        # Tests whose own trace is unusable stay in the incoming selection.
        self.opaque = data["failed"] | data["missing"]
        self.blocked = self.opaque | data["spawn"]
        for mask in data["opens"].values():
            self.blocked |= mask
        self.cpu = data.get("grouped_cpu") or data["cpu"]
        self.by_name = {}
        for key in list(self.masks) + list(self.preload):
            self.by_name.setdefault((key[0], key[1]), []).append(key)
        self.ready = True
        self.stats = {}

    def source(self, path):
        with open(self.base / path, encoding="utf-8", newline="") as f:
            return f.read()

    def analyze(self, text):
        """Return (mask of tests that may change, analyses) or None when unsupported."""
        if not self.ready or not text.endswith("\n"):
            return None
        try:
            files = split_diff(text, physical=True)
        except UnknownChange:
            return None
        mask = self.opaque
        results = {}
        changed = set()
        for path, hunks in files.items():
            if not (path.startswith("sqlglot/") and path.endswith(".py")):
                return None
            try:
                old = self.source(path)
                new = patched(old, hunks, physical=True)
                result = zpatch.analyze(path, old, new, str(self.base / path))
            except (OSError, UnicodeError, UnknownChange, zpatch.Unsupported, ValueError,
                    RecursionError, MemoryError, TypeError):
                return None
            keys = list(result["changed"])
            for name, _, _ in result["defaults"]:
                found = self.by_name.get((path, name))
                if not found:
                    # A default can only matter if the function runs. Unknown name: decline.
                    return None
                keys.extend(found)
            for key in keys:
                if key in self.preload:
                    return None
                mask |= self.masks.get(key, 0)
                changed.add(key)
            mask |= self.opens.get(path, 0)
            results[path] = result
        return mask, results, changed


WALL0 = time.monotonic()
LONG_MS = 3000


class Execute:
    """Run short candidates on the patched code when the possible saving pays for the CPU."""

    def __init__(self, swap, scheduler, base, ids, n_patches):
        self.swap = swap
        self.scheduler = scheduler
        limit = 180.0 if n_patches <= 40 else 270.0
        self.deadline = WALL0 + 0.45 * limit
        self.remaining = 0.8 * n_patches
        self.zygote = None
        if swap.ready:
            from zexec import Zygote
            self.zygote = Zygote(base, ids, self.deadline)
        self.cpu = swap.cpu
        self.stats = {}
        self.tpc = None
        try:
            data = marshal.loads(lzma.decompress(Path(swap.base_map).with_name("ztpc.xz").read_bytes()))
            self.tpc = data
            self.tpc_setup = set(map(tuple, data["setup"]))
            self.tpc_units = {}
            for name, (number, units) in data["tests"].items():
                if swap.ids[number] != data.get("ids", {}).get(name, swap.ids[number]):
                    continue
                coded = [(set(map(tuple, c)), d, t) for c, d, t in units]
                covered = set().union(*(c for c, _, _ in coded))
                self.tpc_units[number] = (name, coded, covered)
        except (OSError, ValueError, EOFError, KeyError, TypeError, lzma.LZMAError):
            self.tpc = None

    def replay_long(self, raw, plan, mask, changed):
        """Drop a TPC executor test when every query that starts changed code digests the same."""
        if self.tpc is None or not plan or time.monotonic() > self.deadline - 10:
            return plan
        s = self.scheduler
        selected = {n for group in plan for n in group}
        for number, (name, units, covered) in self.tpc_units.items():
            if number not in selected or changed & self.tpc_setup:
                continue
            bit = 1 << number
            inside = {k for k in changed if self.swap.masks.get(k, 0) & bit}
            if inside - covered:
                self.stats[name + "_outside"] = True
                continue
            affected = [i for i, (c, _, _) in enumerate(units) if c & changed]
            if not affected or any(units[i][1] is None for i in affected):
                continue
            estimate = 0.1 + sum(units[i][2] for i in affected)
            rest = selected - {number}
            after = s.repack(rest, plan) if rest else []
            upside = (s.cost(plan) - s.cost(after)) / 1000
            self.stats[name + "_queries"] = len(affected)
            self.stats[name + "_estimate"] = round(estimate, 3)
            if estimate > 0.7 * upside or estimate > self.remaining:
                continue
            wall = min(self.deadline - time.monotonic(), 10.0 + 3.0 * estimate)
            digests = self.zygote.tpc(raw, [(name, i) for i in affected], 2.0 * estimate + 1.0, wall)
            self.remaining -= estimate
            if digests is None or len(digests) != len(affected):
                self.stats[name + "_replay"] = "incomplete"
                continue
            same = all(d == units[i][1] for d, i in zip(digests, affected))
            self.stats[name + "_replay"] = "same" if same else "changed"
            if same:
                selected = rest
                plan = after
        return plan

    def refine(self, raw, plan, mask):
        self.stats = {}
        if self.zygote is None or not plan or time.monotonic() > self.deadline - 5:
            return plan
        s = self.scheduler
        selected = {n for group in plan for n in group}
        short = {n for n in selected if s.durations[n] < LONG_MS and mask >> n & 1
                 and not self.swap.blocked >> n & 1}
        if not short:
            return plan
        after = s.repack(selected - short, plan) if selected - short else []
        upside = (s.cost(plan) - s.cost(after)) / 1000
        estimate = 0.03 + sum(self.cpu[n] for n in short)
        self.stats = {"swap_exec_candidates": len(short), "swap_exec_estimate": round(estimate, 3),
                      "swap_exec_upside": round(upside, 3)}
        if estimate > 0.6 * upside or estimate > self.remaining:
            return plan
        tests = sorted(short)
        limits = {n: 1.0 + 4.0 * self.cpu[n] for n in tests}
        wall = min(self.deadline - time.monotonic(), 2.0 + 4.0 * estimate)
        begin = _cpu()
        result = self.zygote.run(raw, tests, limits, 2.0 * estimate + 0.5, wall)
        spent = _cpu() - begin
        self.remaining -= max(spent, estimate)
        if result is None:
            self.stats["swap_exec_failed"] = True
            return plan
        failed, done = result
        passed = done - failed
        self.stats.update(swap_exec_passed=len(passed), swap_exec_failing=len(failed))
        if not passed:
            return plan
        keep = selected - passed
        return s.repack(keep, plan) if keep else []

    def close(self):
        if self.zygote is not None:
            self.zygote.close()


def _cpu():
    t = os.times()
    return t.user + t.system + t.children_user + t.children_system
