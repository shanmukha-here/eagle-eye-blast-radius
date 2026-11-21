"""Index exact comment-filtered fixture readers and keep every raw reader."""

import json
import sys
import zlib
from collections import defaultdict
from pathlib import Path


def main(trace, profile, output):
    ids = json.loads(zlib.decompress(profile.read_bytes()))["ids"]
    owners = {"*": (1 << len(ids)) - 1}
    for i, test in enumerate(ids):
        owners[test] = 1 << i
        module = test.split("::", 1)[0]
        owners[module] = owners.get(module, 0) | (1 << i)
    docs = [json.loads(p.read_bytes()) for p in sorted(trace.glob("fixture-*.json"))]
    if [sorted(row["items"]) for row in docs if row["items"]] != [ids] or len(docs) < 2:
        raise ValueError("incomplete clean fixture observation run")
    observed = defaultdict(set)
    for doc in docs:
        for path, events in doc["events"].items():
            observed[path].update(events)
    masks = {}
    for path, events in observed.items():
        if "filtered" in events:
            mask = 0
            for event in events - {"filtered"}:
                mask |= owners[event]
            masks[path] = format(mask, "x")
    output.write_text(json.dumps(masks, sort_keys=True, separators=(",", ":")) + "\n")
    print(len(masks), "observed fixtures,", sum(int(v, 16) == 0 for v in masks.values()), "without raw reads")


if __name__ == "__main__":
    main(*(Path(arg) for arg in sys.argv[1:]))
