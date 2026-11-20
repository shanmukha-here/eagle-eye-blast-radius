"""Schedule numbered tests using the impact map of the pinned source tree."""
import json
import math
import sys
import time
import zlib
from functools import lru_cache
from pathlib import Path

from impact import Impact
from refine import Details


class Scheduler:
    def __init__(self, tests, profile_ids):
        self.durations = {t["number"]: math.floor(t["seconds"] * 1000 + 0.5) for t in tests}
        self.order = sorted(self.durations, key=lambda n: (-self.durations[n], n))
        bits = {name: 1 << i for i, name in enumerate(profile_ids)}
        self.bits = {t["number"]: bits.get(t["id"], 0) for t in tests}

    @lru_cache(maxsize=128)
    def schedule(self, mask):
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


def main(data=Path("/data"), base=Path("/base"), map_path=Path(__file__).with_name("base.z")):
    tests = json.loads((data / "tests.json").read_bytes())["tests"]
    profile = json.loads(zlib.decompress(map_path.read_bytes()))
    data_map = json.loads(zlib.decompress(map_path.with_name("details.z").read_bytes()))
    impact = Impact(base, profile, Details(data_map, profile["ids"], base))
    scheduler = Scheduler(tests, profile["ids"])
    deadline = time.monotonic() + 145
    for path in sorted((data / "commits").glob("*.patch")):
        mask = impact.full
        if time.monotonic() < deadline and path.stat().st_size <= 1_000_000:
            try:
                mask = impact.select(path.read_text("utf-8"))
            except UnicodeError:
                pass
        workers = scheduler.schedule(mask)
        sys.stdout.write('{"commit":' + json.dumps(path.stem) + ',"workers":' + workers + '}\n')


if __name__ == "__main__":
    main()
