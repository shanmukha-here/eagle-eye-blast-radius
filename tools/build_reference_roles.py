"""Certify fields read only inside the exact test reference boundary on the clean pin."""

import json
import sys
import zlib
from collections import defaultdict
from pathlib import Path


def main(source, output):
    targets = {"test_execute_tpcds": "tpcds", "test_optimized_tpch": "tpch"}
    docs = [json.loads(zlib.decompress(path.read_bytes())) for path in sorted(source.glob("access-*.z"))]
    refs = defaultdict(set)
    outside = defaultdict(set)
    sessions = [set(items) for items, _ in docs if items]
    expected = set(json.loads((source / "refs-complete.json").read_bytes()))
    if sessions != [expected] or set(node.rsplit("::", 1)[-1] for node in expected) != targets.keys():
        raise ValueError("incomplete standalone clean tests")
    if len(docs) < 2:
        raise ValueError("missing executor worker observations")
    for _, events in docs:
        for owner, reads in events.items():
            short = owner.rsplit("::", 1)[-1]
            fields = {key for key in reads if key.startswith("field:")}
            if owner.startswith("reference:") and short in targets:
                refs[targets[short]].update(fields)
            else:
                outside[targets.get(short, "*")].update(fields)
    data = {suite: sorted(refs[suite] - outside[suite] - outside["*"]) for suite in targets.values()}
    output.write_text(json.dumps(data, sort_keys=True, separators=(",", ":")) + "\n")
    print({k: len(v) for k, v in data.items()})


if __name__ == "__main__":
    main(*(Path(arg) for arg in sys.argv[1:]))
