"""Build zcase.xz from forward and reverse zcase.py traces of the pinned source.

Per fixture test that passed in both orders, with at least MIN_SECONDS recorded: the fixture
cases (file, index) in loader order, each case's traced CPU and the outside CPU, and for every
code key and line the cases that started or ran it. A key or line that ran outside any case,
or whose owning cases differ between the two orders, is listed as "bad": the stage may not
replay cases for a change there.

usage: zcasemap.py TESTS_JSON FORWARD_DIR REVERSE_DIR OUT [MIN_SECONDS]
"""

import glob
import json
import lzma
import marshal
import os
import sys


def load(directory):
    out = {}
    for path in sorted(glob.glob(os.path.join(directory, "shard-*.marshal"))):
        with open(path, "rb") as f:
            out.update(marshal.load(f))
    return out


def owners(record):
    codes, lines = {}, {}
    for case, (keys, ran, _) in record["cases"].items():
        for k in keys:
            codes.setdefault(tuple(k), set()).add(case)
        for k, line in ran:
            lines.setdefault((tuple(k), line), set()).add(case)
    return codes, lines


def main():
    manifest, forward, reverse, out = sys.argv[1:5]
    minimum = float(sys.argv[5]) if len(sys.argv) > 5 else 0.0
    tests = json.load(open(manifest))["tests"]
    ids = [t["id"] for t in tests]
    seconds = {t["id"]: t["seconds"] for t in tests}
    F, R = load(forward), load(reverse)
    keys, key_index = [], {}

    def index(k):
        if k not in key_index:
            key_index[k] = len(keys)
            keys.append(k)
        return key_index[k]

    result = {}
    for test, f in sorted(F.items()):
        r = R.get(test)
        if r is None or f["failed"] or r["failed"] or seconds.get(test, 0) < minimum:
            continue
        cases = sorted(c for c in f["cases"] if c is not None)
        if cases != sorted(c for c in r["cases"] if c is not None):
            continue
        number = {c: i for i, c in enumerate(cases)}
        fc, fl = owners(f)
        rc, rl = owners(r)
        bad_codes = {k for k in set(fc) | set(rc)
                     if fc.get(k) != rc.get(k) or None in fc.get(k, ()) or None in rc.get(k, ())}
        bad_lines = {k for k in set(fl) | set(rl)
                     if fl.get(k) != rl.get(k) or None in fl.get(k, ()) or None in rl.get(k, ())}

        def masks(table, bad, line):
            m = {}
            for k, owned in table.items():
                if k in bad:
                    continue
                kk = (index(k[0]), k[1]) if line else index(k)
                m[kk] = sum(1 << number[c] for c in owned)
            return m

        result[ids.index(test)] = {
            "cases": cases,
            "cpu": [f["cases"][c][2] for c in cases],
            "outside_cpu": f["cases"].get(None, ((), (), 0.0))[2],
            "codes": masks(fc, bad_codes, False),
            "lines": masks(fl, bad_lines, True),
            "bad_codes": sorted(index(k) for k in bad_codes),
            "bad_lines": sorted((index(k[0]), k[1]) for k in bad_lines),
        }
    data = {"ids": ids, "keys": keys, "tests": result}
    blob = lzma.compress(marshal.dumps(data), preset=9 | lzma.PRESET_EXTREME)
    with open(out, "wb") as f:
        f.write(blob)
    print("tests", len(result), "keys", len(keys), "bytes", len(blob), file=sys.stderr)


if __name__ == "__main__":
    main()
