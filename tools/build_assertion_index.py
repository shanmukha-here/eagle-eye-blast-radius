"""Compile a successful clean assertion trace into CSR arrays."""
from array import array
import base64
from collections import Counter, defaultdict
import json
import marshal
from pathlib import Path
import sys
import zlib

from assertion_index import ELIGIBLE, HEADER, KEY, MAGIC, REPLAYABLE, UNIT


def main(trace, output):
    report = json.loads((trace / "manifest.json").read_bytes())
    if report["exit"] or not report["ids"]:
        raise ValueError("The clean test trace did not pass")
    code = report["code"]
    opaque = report["opaque"]
    table = defaultdict(list)
    query_sites = defaultdict(set)
    tpcds = output.with_name("tpcds_sql.json")
    executor_map = output.with_name("replay_index.z")
    if tpcds.exists() and executor_map.exists():
        query_rows = json.loads(tpcds.read_bytes())["rows"]
        executor = json.loads(zlib.decompress(executor_map.read_bytes()))
        cases = [row[0] for row in executor["cases"]]
        for site, (optimize, execute) in executor["uses"].items():
            for i in set(optimize) | set(execute):
                query_sites[cases[i]].add("c:" + site)
    else:
        query_rows = []
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
            if unit and unit[0] in ("direct", "block"):
                kind, recipe, bindings = unit
                mode = "eval" if kind == "direct" else "exec"
                program = compile(recipe, "<clean assertion>", mode, dont_inherit=True)
                dump = marshal.dumps((kind, program, bindings))
            query_key = None
            if query_rows and name.endswith("::test_execute_tpcds") and unit and unit[0] == "subtest":
                args = unit[1]
                if len(args) == 1 and isinstance(args[0], str) and args[0].startswith("tpc-ds "):
                    slot = int(args[0].removeprefix("tpc-ds ")) - 1
                    query_key = query_rows[slot][-1]
                    if query_key is not None:
                        unit = ("query", slot, query_key)
                        dump = marshal.dumps(unit)
            if flags and unit and unit[0] in ("helper", "direct", "block") and not hazards:
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
            for key in query_sites.get(query_key, ()):
                table[key].append(index)
                table["m:" + key.split(":", 2)[1]].append(index)
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
