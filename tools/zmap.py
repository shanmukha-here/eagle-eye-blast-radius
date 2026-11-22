"""Combine ztrace shards into the runtime map. Several trace directories (for example from
different hash seeds) are united: a test is a candidate for a code object if any run started it.

Sequential traces (all tests in manifest order in one interpreter) are united too. A code
object that some test starts only after earlier tests changed shared state, and never in its
own fork, is marked state-sensitive: edits to it fall back to the existing selection.

usage: zmap.py TESTS_JSON FORK_DIR[,FORK_DIR...] OUT [CPU_TSV] [SEQ_DIR[,SEQ_DIR...]]
"""

import json
import lzma
import marshal
import sys
from pathlib import Path

manifest, shard_dir, out = sys.argv[1:4]
grouped = {}
if len(sys.argv) > 4:
    import csv
    for row in csv.DictReader(open(sys.argv[4]), delimiter="\t"):
        grouped[row["id"]] = float(row["group_cpu_s"])
ids = [t["id"] for t in json.load(open(manifest))["tests"]]
number = {test_id: i for i, test_id in enumerate(ids)}
runs = []
collect = set()
broken = set()
for directory in shard_dir.split(","):
    records = {}
    shards = sorted(Path(directory).glob("shard-*.marshal"))
    for path in shards:
        data = marshal.loads(path.read_bytes())
        collect.update(map(tuple, data["<collect>"][0]))
        broken.update(data["<broken>"])
        records.update({k: v for k, v in data.items() if not k.startswith("<")})
    runs.append(records)

fork_codes = {}
for records in runs:
    for t, r in records.items():
        if isinstance(r, dict):
            fork_codes.setdefault(t, set()).update(map(tuple, r["codes"]))
sensitive = set()
if len(sys.argv) > 5:
    for directory in sys.argv[5].split(","):
        records = {}
        for path in sorted(Path(directory).glob("shard-*.marshal")):
            data = marshal.loads(path.read_bytes())
            records.update({k: v for k, v in data.items() if not k.startswith("<")})
        for t, r in records.items():
            if isinstance(r, dict):
                sensitive.update(set(map(tuple, r["codes"])) - fork_codes.get(t, set()))
        runs.append(records)

keys = {}
masks = []
line_masks = {}
spawn = 0
failed = 0
missing = 0
opens = {}
cpu = [0.0] * len(ids)
for test_id, rec in ((t, r) for records in runs for t, r in records.items()):
    n = number[test_id]
    bit = 1 << n
    if not isinstance(rec, dict):
        missing |= bit
        continue
    if rec["spawn"]:
        spawn |= bit
    if rec["failed"]:
        failed |= bit
    cpu[n] = max(cpu[n], rec["cpu"] + rec["child_cpu"])
    for path in rec["opens"]:
        if path.endswith(".py"):
            opens[path] = opens.get(path, 0) | bit
    for k in rec["codes"]:
        i = keys.setdefault(tuple(k), len(keys))
        if i == len(masks):
            masks.append(0)
        masks[i] |= bit
    for k, lines in rec["lines"].items():
        f = k[0]
        d = line_masks.setdefault(f, {})
        for line in lines:
            d[line] = d.get(line, 0) | bit
for records in runs:
    for test_id in ids:
        if test_id not in records:
            missing |= 1 << number[test_id]

preload = sorted(collect)
data = {
    "ids": ids,
    "keys": list(keys),
    "masks": masks,
    "preload": preload,
    "spawn": spawn,
    "failed": failed,
    "missing": missing,
    "opens": opens,
    "cpu": cpu,
    "sensitive": sorted(sensitive),
    "grouped_cpu": [grouped.get(t, c) for t, c in zip(ids, cpu)],
}
raw = marshal.dumps(data)
Path(out).write_bytes(lzma.compress(raw, preset=9 | lzma.PRESET_EXTREME))
lines_raw = marshal.dumps(line_masks)
Path(out + ".lines").write_bytes(lzma.compress(lines_raw, preset=9 | lzma.PRESET_EXTREME))
print("sensitive", len(sensitive), "runs", len(runs), "tests", len(runs[0]), "keys", len(keys), "preload", len(preload), "spawn", bin(spawn).count("1"),
      "failed", bin(failed).count("1"), "missing", bin(missing).count("1"),
      "bytes", len(raw), Path(out).stat().st_size, "lines bytes", len(lines_raw), Path(out + ".lines").stat().st_size,
      file=sys.stderr)
