"""Remap scalar inputs seen in executor workers to the clean query index."""

import json
import sys
import zlib
from collections import defaultdict
from pathlib import Path


def main(source, index, output):
    ref = json.loads(zlib.decompress(index.read_bytes()))
    ids = {row[0]: i for i, row in enumerate(ref["cases"])}
    inputs = [json.loads(zlib.decompress(path.read_bytes()))
              for path in sorted(source.glob("value-trace-*.z"))]
    if [d["items"] for d in inputs if d["items"]] != [[ref["test"]]] or len(inputs) < 2:
        raise ValueError("incomplete worker observation run")
    merged = defaultdict(dict)
    skipped = set()
    for report in inputs:
        for site, groups in report["values"].items():
            for owner, rows in groups.items():
                if not owner.startswith("case:"):
                    skipped.add(site)
                    continue
                bit = 1 << ids[owner[5:]]
                for row in rows:
                    key = json.dumps(row, separators=(",", ":"))
                    merged[site][key] = merged[site].get(key, 0) | bit
    result = {site: [[json.loads(case), bits] for case, bits in sorted(cases.items())]
              for site, cases in merged.items() if site not in skipped}
    output.write_bytes(zlib.compress(json.dumps(result, sort_keys=True, separators=(",", ":")).encode(), 9))
    print(len(result), "verified executor expression sites")


if __name__ == "__main__":
    main(*(Path(arg) for arg in sys.argv[1:]))
