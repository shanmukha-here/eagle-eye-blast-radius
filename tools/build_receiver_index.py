"""Combine clean receiver dispatch and qualified member reads into a flat index."""
from collections import defaultdict
from functools import lru_cache
import json
from pathlib import Path
import sys
import zlib

from assertion_index import AssertionIndex, HEADER, KEY
from observation_file import build


def main(profile, dispatch, reflection, output):
    ids = json.loads(zlib.decompress(profile.read_bytes()))["ids"]
    tests = defaultdict(int)
    for i, name in enumerate(ids):
        tests[name] |= 1 << i
        tests[name.partition("::")[0]] |= 1 << i
    tests["*"] = (1 << len(ids)) - 1
    rows = defaultdict(int)
    plain = set()
    for trace in reflection.glob("members-*.json"):
        data = json.loads(trace.read_bytes())
        plain.update(data["plain"])
        for name, readers in data["reads"].items():
            for test in readers:
                rows["r:" + name] |= tests[test]

    index = AssertionIndex(dispatch)
    test_at = lru_cache(maxsize=None)(index.test)
    for i in range(index.key_count):
        start, size, _, _ = KEY.unpack_from(index.data, HEADER.size + KEY.size * i)
        name = index.read(start, size).decode()
        if name.startswith("d:"):
            rows[name] = 0
            for unit in index.hits(name):
                rows[name] |= tests[test_at(unit)]
    if not plain or not rows:
        raise ValueError("missing clean receiver trace")
    for name in plain:
        rows["plain:" + name] = 1
    build(output, {key: ((None, bits),) for key, bits in rows.items()})
    print(f"compiled {len(plain)} plain classes and {len(rows)} receiver keys")


if __name__ == "__main__":
    main(*(Path(arg) for arg in sys.argv[1:]))
