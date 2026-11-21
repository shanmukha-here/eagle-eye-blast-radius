"""Build a small access index from complete clean-base traces and pinned source."""

import argparse
import json
import re
import sys
import zlib
from collections import defaultdict
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dependencies import name_bits  # noqa: E402


def pack(directory, source, base):
    ids = base["ids"]
    masks = {"*": (1 << len(ids)) - 1}
    for i, name in enumerate(ids):
        masks[name] = 1 << i
        f = name.split("::", 1)[0]
        masks[f] = masks.get(f, 0) | (1 << i)
    sessions = []
    observed = defaultdict(int)
    paths = sorted(directory.glob("access-*.z"))
    for path in paths:
        tests, events = json.loads(zlib.decompress(path.read_bytes()))
        if tests:
            sessions.append(sorted(tests))
        for context, keys in events.items():
            for key in keys:
                observed[key] |= masks[context]
    if sessions != [ids] or len(paths) < 2 or not observed["pytest:collect"]:
        raise ValueError(
            "missing main session, child traces or expected directory observation"
        )
    # A new module must have a name absent from the pinned source and fixtures.
    identifiers = set()
    for folder in ("sqlglot", "tests"):
        for path in sorted((source / folder).rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts:
                identifiers.update(
                    re.findall(rb"[A-Za-z_][A-Za-z_0-9]*", path.read_bytes())
                )
    bitmap = bytearray(131072)
    for word in identifiers:
        for index in name_bits(word, len(bitmap)):
            bitmap[index // 8] |= 1 << (index % 8)
    return {
        "events": {key: format(bits, "x") for key, bits in sorted(observed.items())},
        "identifiers": bitmap.hex(),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("traces", "root", "base", "out"):
        parser.add_argument(name, type=Path)
    a = parser.parse_args()
    result = pack(a.traces, a.root, json.loads(zlib.decompress(a.base.read_bytes())))
    a.out.write_bytes(
        zlib.compress(json.dumps(result, separators=(",", ":")).encode(), 9)
    )
