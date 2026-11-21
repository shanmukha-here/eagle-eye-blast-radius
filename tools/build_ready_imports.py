"""Merge import-readiness witnesses after the clean suite has passed."""

import json
import sys
import zlib
from pathlib import Path


def main(source, profile, output):
    tests = json.loads(zlib.decompress(profile.read_bytes()))["ids"]
    inputs = [json.loads(path.read_bytes()) for path in sorted(source.glob("ready-*.json"))]
    sessions = [sorted(x["items"]) for x in inputs if x["items"]]
    if sessions != [tests] or len(inputs) < 2:
        raise ValueError("incomplete clean test and worker observations")
    merged = {}
    for record in inputs:
        for site, ready in record["observations"].items():
            merged[site] = merged.get(site, True) and ready
    output.write_text(json.dumps(sorted(k for k, ready in merged.items() if ready)) + "\n")
    print(len(merged), "observed guards,", sum(merged.values()), "always initialized")


if __name__ == "__main__":
    main(*(Path(value) for value in sys.argv[1:]))
