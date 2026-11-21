"""Merge clean integration coverage with its validated query and optimizer fingerprints."""

import argparse
import json
import zlib
from collections import defaultdict
from pathlib import Path


def index(directory):
    uses, outcomes, checkpoints = (defaultdict(set) for _ in range(3))
    tests = []
    for path in sorted(directory.glob("case-*.json")):
        ran, *parts = json.loads(path.read_bytes())
        tests.extend(ran)
        for target, rows in zip((uses, outcomes, checkpoints), parts):
            for key, values in rows.items():
                target[key].update(values)
    if len(tests) != 1 or not outcomes or len(list(directory.glob("case-*.json"))) < 2:
        raise ValueError("incomplete integration trace")
    keys = sorted(k for k, values in outcomes.items() if len(values) == 1)
    ids = {key: i for i, key in enumerate(keys)}
    cases = []
    for key in keys:
        opt = checkpoints[key]
        cases.append([key, next(iter(outcomes[key])), next(iter(opt)) if len(opt) == 1 and "unknown" not in opt else None])
    coverage = {}
    for location, tags in sorted(uses.items()):
        if "outside" in tags:
            continue
        phases = [s.split(":", 1) for s in tags]
        if not all(key in ids for _, key in phases):
            continue
        query = {ids[key] for phase, key in phases if phase == "query"}
        opt = {ids[key] for phase, key in phases if phase == "opt"}
        coverage[location] = [sorted(opt - query), sorted(query)]
    return {"test": tests[0], "cases": cases, "uses": coverage}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("out", type=Path)
    a = parser.parse_args()
    a.out.write_bytes(zlib.compress(json.dumps(index(a.directory), separators=(",", ":")).encode(), 9))
