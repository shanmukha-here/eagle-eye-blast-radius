"""Compile a successful clean assertion trace into CSR arrays."""
from array import array
import base64
from collections import Counter, defaultdict
import json
import marshal
from pathlib import Path
import sys

from assertion_index import ELIGIBLE, HEADER, KEY, MAGIC, REPLAYABLE, UNIT


def main(trace, output):
    report = json.loads((trace / "manifest.json").read_bytes())
    if report["exit"] or not report["ids"]:
        raise ValueError("The clean test trace did not pass")
    code = report["code"]
    opaque = report["opaque"]
    table = defaultdict(list)
    scopes = {(path, qual): line for path, line, qual in code if line >= 0}
    for ident in report.get("startup", []):
        path, line, qual = code[ident]
        if ".<locals>." in qual:
            qual = qual.split(".<locals>.", 1)[0]
            line = scopes.get((path, qual), line)
        table[f"b:{path}:{line}:{qual}"].append(0)
    units = bytearray()
    by_kind = Counter()
    blob = bytearray()
    names = {}
    counts = Counter()

    def store(data):
        offset = len(blob)
        blob.extend(data)
        return offset, len(data)

    def name_ref(name):
        if name not in names:
            names[name] = store(name.encode())
        return names[name]

    with (trace / "units.jsonl").open() as source:
        hazardous = set()
        for line in source:
            name, _, _, hazards = json.loads(line)
            if hazards:
                hazardous.add(name)
        source.seek(0)
        for line in source:
            name, payload, identifiers, hazards = json.loads(line)
            dump = base64.b85decode(payload)
            unit = marshal.loads(dump)
            flags = ELIGIBLE if name not in opaque and name not in hazardous else 0
            if flags and unit and unit[0] in ("helper", "direct") and not hazards:
                flags |= REPLAYABLE
            counts["units"] += 1
            counts["replayable"] += bool(flags & REPLAYABLE)
            by_kind["outside" if unit is None else unit[0]] += 1
            if flags & REPLAYABLE:
                by_kind["replayable:" + unit[0]] += 1
            counts.update(f"hazard:{hazard}" for hazard in hazards)
            index = len(units) // UNIT.size
            units.extend(UNIT.pack(*name_ref(name), *store(dump), flags))
            table["t:" + name].append(index)
            visited = set()
            for ident in identifiers:
                path, line, qual = code[ident]
                if line < 0:
                    table[f"d:{path}:{qual}"].append(index)
                else:
                    table[f"c:{path}:{line}:{qual}"].append(index)
                if ".<locals>." in qual:
                    parent = qual.split(".<locals>.", 1)[0]
                    first = scopes.get((path, parent))
                    if first is not None:
                        table[f"s:{path}:{first}:{parent}"].append(index)
                visited.add(path)
            for path in visited:
                table["m:" + path].append(index)
    key_rows, edges = bytearray(), array("I")
    if sys.byteorder != "little" or edges.itemsize != 4:
        raise RuntimeError("Build the index on little-endian Linux amd64")
    for key, members in sorted(table.items()):
        members = sorted(set(members))
        key_rows.extend(KEY.pack(*name_ref(key), len(edges), len(members)))
        edges.extend(members)
    with output.open("wb") as f:
        f.write(HEADER.pack(MAGIC, len(table), len(edges), len(units) // UNIT.size))
        f.write(key_rows)
        edges.tofile(f)
        f.write(units)
        f.write(blob)
    print(dict(counts), dict(by_kind), "test names", len(report["ids"]), "indexed bytes", output.stat().st_size)


if __name__ == "__main__":
    main(*(Path(value) for value in sys.argv[1:]))
