"""Pool repeated line masks while preserving explicit outside-of-query execution."""
import json
import sys
import zlib
from collections import defaultdict
from pathlib import Path


def pack(directory, case_index):
    cases = json.loads(zlib.decompress(case_index.read_bytes()))
    refs = {row[0]: i for i, row in enumerate(cases["cases"])}
    sessions, regions = [], defaultdict(set)
    files = sorted(directory.glob("case-*.json"))
    for f in files:
        ran, coverage, outcomes, _ = json.loads(f.read_bytes())
        sessions.extend(ran)
        if any(key not in refs for key in outcomes):
            raise ValueError("query index changed")
        for key, contexts in coverage.items():
            if "@" in key:
                regions[key].update(contexts)
    if sessions != [cases["test"]] or len(files) < 2 or not regions:
        raise ValueError("incomplete query line trace")
    pool, indices, mapping = [], {}, {}
    for loc, events in sorted(regions.items()):
        opt, whole = set(), set()
        if "outside" in events:
            key = (-1, -1)
        else:
            for kind in events:
                phase, query = kind.split(":", 1)
                if phase not in ("opt", "query") or query not in refs:
                    raise ValueError(kind)
                (opt if phase == "opt" else whole).add(refs[query])
            key = (sum(1 << i for i in opt - whole), sum(1 << i for i in whole))
        if key not in indices:
            indices[key] = len(pool)
            pool.append(key)
        mapping[loc] = indices[key]
    return {"pool": pool, "locations": mapping}


if __name__ == "__main__":
    data = pack(Path(sys.argv[1]), Path(sys.argv[2]))
    Path(sys.argv[3]).write_bytes(zlib.compress(json.dumps(data, separators=(",", ":")).encode(), 9))
