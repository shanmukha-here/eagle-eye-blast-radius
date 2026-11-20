"""Merge per-process traces from an unmodified checkout into a portable ID index."""
import argparse
import json
import zlib
from collections import defaultdict
from pathlib import Path


def build(directory):
    traces = [json.loads(zlib.decompress(p.read_bytes())) for p in sorted(directory.glob("trace-*.z"))]
    sessions = [r["items"] for r in traces if r["items"]]
    if len(sessions) != 1 or len(traces) < 2:
        raise ValueError("expected one successful parent trace and child traces")
    ids = sorted(sessions[0])
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate pytest IDs")
    universe = (1 << len(ids)) - 1
    masks = {"*": universe}
    for index, node in enumerate(ids):
        bit = 1 << index
        masks[node] = bit
        filename = node.split("::", 1)[0]
        masks[filename] = masks.get(filename, 0) | bit
    data = defaultdict(dict)
    visitors = defaultdict(int)
    for trace in traces:
        for owner, hits in trace["hits"].items():
            mask = masks[owner]
            for filename, line in hits:
                values = data[filename]
                values[line] = values.get(line, 0) | mask
                if owner != "*" and line:
                    visitors[filename] |= mask
    files = {
        path: {str(k): format(v, "x") for k, v in sorted(rows.items())}
        for path, rows in sorted(data.items())
    }
    return {"ids": ids, "files": files,
            "visitors": {p: format(v, "x") for p, v in sorted(visitors.items())}}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trace_directory", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    raw = json.dumps(build(args.trace_directory), separators=(",", ":")).encode()
    args.output.write_bytes(zlib.compress(raw, 9))
