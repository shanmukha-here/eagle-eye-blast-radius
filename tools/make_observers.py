"""Merge clean class and enum observations for individually numbered pytest contexts."""
import argparse
import json
import zlib
from collections import defaultdict
from pathlib import Path


def build(directories, manifest):
    ids = json.loads(zlib.decompress(manifest.read_bytes()))["ids"]
    full = (1 << len(ids)) - 1
    owners = {"*": full}
    for i, name in enumerate(ids):
        owners[name] = 1 << i
        filename = name.split("::", 1)[0]
        owners[filename] = owners.get(filename, 0) | (1 << i)
    data = defaultdict(int)
    for folder, prefix in directories:
        sessions = []
        traces = sorted(folder.glob("access-*.z"))
        for filename in traces:
            tests, observations = json.loads(zlib.decompress(filename.read_bytes()))
            if tests:
                sessions.append(sorted(tests))
            for owner, keys in observations.items():
                for key in keys:
                    if key.startswith(prefix):
                        data[key] |= owners[owner]
        if sessions != [ids] or len(traces) < 2:
            raise ValueError("incomplete main or child observation")
    return {key: format(value, "x") for key, value in sorted(data.items())}


def guard_sites(directory, manifest, detail_file):
    ids = json.loads(zlib.decompress(manifest.read_bytes()))["ids"]
    owners = {"*": (1 << len(ids)) - 1}
    for i, name in enumerate(ids):
        owners[name] = 1 << i
        path = name.split("::", 1)[0]
        owners[path] = owners.get(path, 0) | (1 << i)
    old = json.loads(zlib.decompress(detail_file.read_bytes()))
    pool = [int(s, 16) for s in old["bits"]]
    seen, unsafe, sessions = defaultdict(int), set(), []
    paths = sorted(directory.glob("operands-*.z"))
    for path in paths:
        tests, contexts = json.loads(zlib.decompress(path.read_bytes()))
        if tests:
            sessions.append(sorted(tests))
        for context, pairs in contexts.items():
            for key, tag in pairs:
                if tag == "none-safe":
                    seen[key] |= owners[context]
                if tag == "unresolved":
                    unsafe.add(key)
    if sessions != [ids] or len(paths) < 2:
        raise ValueError("incomplete clean operand run")
    out = {}
    for key, mask in seen.items():
        prior = old["operands"].get(key, {}).get("unresolved")
        if key not in unsafe and prior is not None and not pool[prior] & ~mask:
            out["guard:" + key] = "1"
    return out


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("components", "enums", "guards", "manifest", "details", "output"):
        parser.add_argument(name, type=Path)
    a = parser.parse_args()
    vals = build([(a.components, "component:"), (a.enums, "enum:")], a.manifest)
    vals.update(guard_sites(a.guards, a.manifest, a.details))
    a.output.write_bytes(zlib.compress(json.dumps(vals, separators=(",", ":")).encode(), 9))
