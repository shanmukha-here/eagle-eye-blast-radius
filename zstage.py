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
        self.lines = {}
        try:
            self.lines = marshal.loads(lzma.decompress(Path(str(map_path) + ".lines").read_bytes()))
        except (OSError, ValueError, EOFError, lzma.LZMAError):
            self.lines = {}
        self.classes = None
        try:
            self.classes = marshal.loads(lzma.decompress(Path(map_path).with_name("zclass.xz").read_bytes()))
        except (OSError, ValueError, EOFError, lzma.LZMAError):
            self.classes = None
        self.inverted = None
        self.by_name = {}
        for key in list(self.masks) + list(self.preload):
            self.by_name.setdefault((key[0], key[1]), []).append(key)
        self.ready = True
        self.stats = {}

    def source(self, path):
        with open(self.base / path, encoding="utf-8", newline="") as f:
            return f.read()

    def _refs(self, name, kind, patched):
        """Files other than ``patched`` whose pinned source has ``name`` as a ``kind``
        (0 identifier, 1 string, 2 compared, 3 non-call reference), plus patched files whose
        new source does."""
        if self.inverted is None:
            self.inverted = inverted = [{}, {}, {}, {}]
            for path, sets in self.classes["files"].items():
                if sets is None:
                    for table in inverted:
                        table.setdefault(None, set()).add(path)
                    continue
                for table, values in zip(inverted, sets):
                    for value in values:
                        table.setdefault(value, set()).add(path)
        found = set(self.inverted[kind].get(name, ())) | set(self.inverted[kind].get(None, ()))
        found -= set(patched)
        for path, sets in patched.items():
            if sets is None or name in sets[kind]:
                found.add(path)
        return found

    def _resolve(self, path, scope, name):
        """Code entries that instances and subclasses of the class resolved ``name`` to on the
        pin, below the class itself, and whether some subclass had no such attribute; None
        when unknown."""
        data = self.classes
        number = data["unique"].get((path, scope))
        if number is None or number < 0:
            return None
        entries, absent = set(), False
        for record_path, mro, own in data["classes"]:
            if number not in mro:
                continue
            at = mro.index(number)
            if any(not isinstance(m, str) and name in data["classes"][m][2] for m in mro[:at]):
                continue
            for m in mro[at + 1:]:
                if isinstance(m, str):
                    if name in data["external"].get(m, ()) or m not in data["external"]:
                        return None
                    continue
                table = data["classes"][m][2]
                if name in table:
                    if table[name] is None:
                        return None
                    entries.add((table[name][0], tuple(table[name][1])))
                    break
            else:
                absent = True
        return entries, absent

    def _members(self, path, result, patched):
        """Keys whose starts cover every test an added or deleted method can affect, or None."""
        if self.classes is None:
            return None
        keys = []
        for scope, name, passthrough in result["added"]:
            if name.startswith("__") or self._refs(name, 1, patched) or self._refs(name, 2, patched):
                return None
            if not scope:
                # A new module function is unseen unless something names it.
                if self._refs(name, 0, patched):
                    return None
                continue
            found = self._resolve(path, scope, name)
            if found is None:
                return None
            entries, absent = found
            if absent and (self._refs(name, 0, patched)
                           or (name.endswith("_sql") and not name.startswith("_"))):
                return None
            if passthrough and not absent and all(kind == "function" for kind, _ in entries):
                # Calls reach the same inherited function with the same arguments.
                continue
            keys.extend(key for _, key in entries)
        for scope, name, key in result["removed"]:
            if name.startswith("__") or self._refs(name, 1, patched) or self._refs(name, 2, patched) \
                    or self._refs(name, 3, patched):
                return None
            keys.append(tuple(key))
        return keys

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
        executable = True
        member_files = {}
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
            if result["added"] or result["removed"]:
                member_files[path] = result["names"]
            results[path] = result
        for path, result in results.items():
            hunks = files[path]
            old = result["old_text"]
            line_keys = {}
            file_lines = self.lines.get(path)
            if file_lines is not None and result["changed"]:
                try:
                    tree = __import__("ast").parse(old)
                    nodes = zpatch.function_nodes(tree)
                    edited, inserts = zpatch.changed_lines(hunks)
                    for key in result["scoped"]:
                        ranges = zpatch.line_ranges(tree, nodes, key, edited, inserts)
                        if ranges is not None:
                            m = 0
                            for a, b in ranges:
                                for line in range(a, b + 1):
                                    m |= file_lines.get(line, 0)
                            line_keys[key] = m
                except (SyntaxError, ValueError, RecursionError, MemoryError):
                    line_keys = {}
            keys = list(result["changed"])
            if result["added"] or result["removed"]:
                # The zygote cannot add or delete methods, so these commits are not executed.
                executable = False
                more = self._members(path, result, member_files)
                if more is None:
                    return None
                keys.extend(more)
            for name, _, _ in result["defaults"]:
                found = self.by_name.get((path, name))
                if not found:
                    # A default can only matter if the function runs. Unknown name: decline.
                    return None
                keys.extend(found)
            for key in keys:
                if key in self.preload:
                    return None
                if key in line_keys:
                    mask |= line_keys[key] & self.masks.get(key, 0)
                else:
                    mask |= self.masks.get(key, 0)
                changed.add(key)
            mask |= self.opens.get(path, 0)
        return mask, results, changed, executable


WALL0 = time.monotonic()
LONG_MS = 3000
# Stages that run tests in the zygote; each is worth its CPU only where it removes workers.
REPLAY_LONG = True
REFINE_SHORT = True


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
        if not REPLAY_LONG or self.tpc is None or not plan or time.monotonic() > self.deadline - 10:
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
            # Traced query CPU includes the tracer; replays use about half.
            estimate = 0.1 + 0.5 * sum(units[i][2] for i in affected)
            rest = selected - {number}
            after = s.repack(rest, plan) if rest else []
            upside = (s.cost(plan) - s.cost(after)) / 1000
            self.stats[name + "_queries"] = len(affected)
            self.stats[name + "_estimate"] = round(estimate, 3)
            if estimate > 0.7 * upside or estimate > self.remaining:
                continue
            wall = min(self.deadline - time.monotonic(), 10.0 + 3.0 * estimate)
            digests = self.zygote.tpc(raw, [(name, i) for i in affected], 2.0 * estimate + 1.0, wall)
            self.remaining -= max(estimate, self.zygote.last_cpu)
            self.stats[name + "_cpu"] = round(self.zygote.last_cpu, 3)
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
        if not REFINE_SHORT or self.zygote is None or not plan or time.monotonic() > self.deadline - 5:
            return plan
        s = self.scheduler
        selected = {n for group in plan for n in group}
        short = {n for n in selected if s.durations[n] < LONG_MS and mask >> n & 1
                 and not self.swap.blocked >> n & 1}
        if not short:
            return plan
        after = s.repack(selected - short, plan) if selected - short else []
        upside = (s.cost(plan) - s.cost(after)) / 1000
        # The grouped profile overstates in-process CPU about threefold.
        estimate = 0.03 + 0.35 * sum(self.cpu[n] for n in short)
        self.stats = {"swap_exec_candidates": len(short), "swap_exec_estimate": round(estimate, 3),
                      "swap_exec_upside": round(upside, 3)}
        if estimate > 0.6 * upside or estimate > self.remaining:
            return plan
        tests = sorted(short)
        limits = {n: 2.0 + 4.0 * self.cpu[n] for n in tests}
        wall = min(self.deadline - time.monotonic(), 2.0 + 4.0 * estimate)
        begin = _cpu()
        result = self.zygote.run(raw, tests, limits, 2.0 * estimate + 0.5, wall)
        self.remaining -= max(self.zygote.last_cpu, estimate)
        self.stats["swap_exec_cpu"] = round(self.zygote.last_cpu, 3)
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
            return getattr(self.zygote, "total_cpu", None)


def _cpu():
    t = os.times()
    return t.user + t.system + t.children_user + t.children_system
