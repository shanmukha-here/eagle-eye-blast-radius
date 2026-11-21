"""Merge a complete clean test run into compact bytecode-offset coverage."""

import argparse
import ast
import json
import zlib
from collections import defaultdict
from functools import lru_cache
from pathlib import Path


def build(traces, ids):
    masks = {"*": (1 << len(ids)) - 1}
    for i, name in enumerate(ids):
        masks[name] = 1 << i
        path = name.split("::", 1)[0]
        masks[path] = masks.get(path, 0) | (1 << i)
    tables = defaultdict(dict)
    sessions = []
    opaque = 0
    files = set()
    for trace in sorted(traces.glob("flow-*.z")):
        tests, contexts, imported, external, entries = json.loads(zlib.decompress(trace.read_bytes()))
        if tests:
            sessions.append(sorted(tests))
        owners = [None] * len(contexts)
        for name, i in contexts.items():
            owners[i] = masks[name]
        for name in external:
            opaque |= masks[name]
        files.update(imported)

        @lru_cache(maxsize=None)
        def translate(word):
            value = 0
            while word and value != masks["*"]:
                bit = word & -word
                value |= owners[bit.bit_length() - 1]
                word ^= bit
            return value

        for path, qualname, line, sig, values in entries:
            row = tables[(path, qualname, line, sig)]
            for offset, val in values.items():
                o = int(offset)
                row[o] = row.get(o, 0) | translate(int(val, 16))
    if sessions != [ids] or len(list(traces.glob("flow-*.z"))) < 2:
        raise ValueError("incomplete clean or child session")
    pool = {0: 0}
    by_file = defaultdict(dict)
    for (path, name, line, sig), offsets in sorted(tables.items()):
        # Run-length encoding of adjacent executed instructions with identical owners.
        row = []
        for off, mask in sorted(offsets.items()):
            word = pool.setdefault(mask, len(pool))
            if not row or word != row[-1][2] or off != row[-1][1] + 2:
                row.append([off, off, word])
            else:
                row[-1][1] = off
        by_file[path][f"{line}:{name}:{sig}"] = row
    return {"files": sorted(files), "opaque": format(opaque, "x"), "pool": [format(v, "x") for v in pool], "code": by_file}


def add_observers(data, operands, source, details, ids):
    observed = defaultdict(set)
    sessions = []
    for path in sorted(operands.glob("operands-*.z")):
        tests, contexts = json.loads(zlib.decompress(path.read_bytes()))
        if tests:
            sessions.append(sorted(tests))
        for rows in contexts.values():
            for key, actual in rows:
                observed[key].add(actual)
    if sessions != [ids]:
        raise ValueError("incomplete comparison trace")
    data["types"] = {k: sorted(v) for k, v in sorted(observed.items())}
    pool = [int(v, 16) for v in details["bits"]]
    mask = pool[details["positions"]]
    for file, lines in details["lines"].items():
        for n in ast.walk(ast.parse((source / file).read_bytes())):
            if isinstance(n, ast.Attribute) and (n.attr.startswith("co_") or n.attr == "__code__"):
                i = lines.get(str(n.lineno))
                if i is not None:
                    mask |= pool[i]
    data["reflect"] = format(mask, "x")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("traces", "base", "operands", "source", "details", "out"):
        parser.add_argument(name, type=Path)
    args = parser.parse_args()
    ids = json.loads(zlib.decompress(args.base.read_bytes()))["ids"]
    result = build(args.traces, ids)
    add_observers(result, args.operands, args.source, json.loads(zlib.decompress(args.details.read_bytes())), ids)
    args.out.write_bytes(zlib.compress(json.dumps(result, separators=(",", ":")).encode(), 9))
