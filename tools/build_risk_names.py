"""Precompute which test source files mention clean symbols."""
import json
from pathlib import Path
import sys
import zlib

from precision_index import build
from risk_features import symbol_words


def main(root, profile, output):
    names = json.loads(zlib.decompress(profile.read_bytes()))["ids"]
    tests = {}
    for i, node in enumerate(names):
        file = node.partition("::")[0]
        tests[file] = tests.get(file, 0) | (1 << i)
    rows = {}
    for file, mask in tests.items():
        path = root / file
        if path.is_file():
            for word in symbol_words(path.read_text()):
                key = "word:" + word
                rows[key] = rows.get(key, 0) | mask
    build(output, rows, (len(names) + 7) // 8)
    print(f"indexed {len(rows)} lexical test references")


if __name__ == "__main__":
    main(*(Path(arg) for arg in sys.argv[1:]))
