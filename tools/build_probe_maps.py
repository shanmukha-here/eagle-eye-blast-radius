"""Merge successful clean suite traces into per-test masks and per-dialect cases."""
import argparse
import json
import zlib
from pathlib import Path

from make_observers import build


def write(path, data):
    path.write_bytes(zlib.compress(json.dumps(data, sort_keys=True, separators=(",", ":")).encode(), 9))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for arg in ("manifest", "lazy", "registry", "memory", "output"):
        parser.add_argument(arg, type=Path)
    args = parser.parse_args()
    values = build([(args.lazy, "lazy:"), (args.registry, "registry:")], args.manifest)
    if not any(k.startswith("registry:key:") for k in values):
        raise ValueError("registry operands were not traced")
    values["registry:verified"] = "1"
    write(args.output / "startup.z", values)
    files = list(args.memory.glob("case-*.json"))
    if len(files) != 1:
        raise ValueError("expected one generator test process")
    tests, coverage, _, _ = json.loads(files[0].read_bytes())
    if tests != ["tests/test_generator.py::TestGenerator::test_generator_memory"]:
        raise ValueError("incomplete generator memory trace")
    cases = {k: sorted(s[8:] for s in contexts) for k, contexts in coverage.items()
             if contexts and all(s.startswith("dialect:") for s in contexts)}
    write(args.output / "memory_index.z", cases)
