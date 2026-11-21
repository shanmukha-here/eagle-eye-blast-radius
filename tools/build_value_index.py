"""Build a test-ID observation index from a complete, passing clean-pin trace."""

import argparse
import json
import zlib
from collections import defaultdict
from pathlib import Path


def load(path):
    return json.loads(zlib.decompress(path.read_bytes()))


def build(source, profile):
    tests = profile["ids"]
    contexts = {"*": (1 << len(tests)) - 1}
    for i, test in enumerate(tests):
        contexts[test] = 1 << i
        module = test.split("::", 1)[0]
        contexts[module] = contexts.get(module, 0) | (1 << i)

    reports = [load(path) for path in sorted(source.glob("value-trace-*.z"))]
    sessions = [sorted(report["items"]) for report in reports if report["items"]]
    if sessions != [tests]:
        raise ValueError("trace must have one complete pytest session")

    defaults = {}
    values = defaultdict(dict)
    for report in reports:
        for site in report["catalog"]:
            defaults.setdefault(site, 0)
        for site, owners in report["defaults"].items():
            for owner in owners:
                defaults[site] = defaults.get(site, 0) | contexts[owner]
        for site, occurrences in report["values"].items():
            for owner, cases in occurrences.items():
                bits = contexts[owner]
                for case in cases:
                    encoded = json.dumps(case, separators=(",", ":"))
                    values[site][encoded] = values[site].get(encoded, 0) | bits
    return {
        "defaults": {k: format(v, "x") for k, v in defaults.items()},
        "values": {site: [[json.loads(case), format(bits, "x")] for case, bits in sorted(cases.items())]
                   for site, cases in values.items()},
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("trace", type=Path)
    parser.add_argument("base", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    model = build(args.trace, load(args.base))
    args.output.write_bytes(zlib.compress(json.dumps(model, sort_keys=True, separators=(",", ":")).encode(), 9))
    print(len(model["defaults"]), "default declarations,", len(model["values"]), "scalar locations")


if __name__ == "__main__":
    main()
