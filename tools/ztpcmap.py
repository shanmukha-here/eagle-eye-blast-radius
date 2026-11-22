"""Pack the per-query TPC traces (several hash seeds) into the runtime file.

usage: ztpcmap.py TESTS_JSON OUT TRACE [TRACE...]

Line coverage comes from traces that recorded it; CPU from those that did not, when given.
Split traces (ztpcsplit.py) are given last, the PYTHONHASHSEED=0 one first.
"""
import json
import lzma
import marshal
import sys
from pathlib import Path

manifest, out, *traces = sys.argv[1:]
ids = [t["id"] for t in json.load(open(manifest))["tests"]]
names = {"tpcds": "tests/test_executor.py::TestExecutor::test_execute_tpcds",
         "tpch": "tests/test_executor.py::TestExecutor::test_execute_tpch"}
runs = [marshal.loads(Path(p).read_bytes()) for p in traces]
# Split traces (ztpcsplit.py) map test names to units directly.
splits = [r for r in runs if "units" not in r]
runs = [r for r in runs if "units" in r]
setup = set()
for r in runs:
    setup.update(map(tuple, r["setup"]))
result = {"setup": sorted(setup), "tests": {}}
for name, test_id in names.items():
    units = []
    line_units = {}
    complete = False
    for i in range(len(runs[0]["units"][name])):
        codes = set()
        digests = set()
        cpu = 0.0
        plain_cpu = 0.0
        for r in runs:
            c, d, t, *rest = r["units"][name][i]
            codes.update(map(tuple, c))
            digests.add(d)
            cpu = max(cpu, t)
            for path, line in (rest[0] if rest else ()):
                table = line_units.setdefault(path, {})
                table[line] = table.get(line, 0) | 1 << i
            if rest:
                complete = True
            else:
                # Line tracing slows queries; prefer CPU from runs that traced starts only.
                plain_cpu = max(plain_cpu, t)
        cpu = plain_cpu or cpu
        units.append((sorted(codes), digests.pop() if len(digests) == 1 else None, cpu))
    result["tests"][name] = (ids.index(test_id), units)
    if complete:
        # {path: {line: mask of the units that ran it}}
        result.setdefault("lines", {})[name] = line_units
if splits:
    result["split"] = {}
    for name in names:
        units = []
        for i in range(len(splits[0][name])):
            rows, pres, cpu = set(), [], 0.0
            for r in splits:
                row_codes, pre, full, cpu_pre, _ = r[name][i]
                rows.update(map(tuple, row_codes))
                pres.append(None if pre.startswith("error:") else pre)
                cpu = max(cpu, cpu_pre)
            # The zygote runs with PYTHONHASHSEED=0, so it compares with the first split
            # trace, which must use seed 0. The other seeds only add row-phase code.
            units.append((sorted(rows), pres[0], cpu))
        result["split"][name] = units
Path(out).write_bytes(lzma.compress(marshal.dumps(result), preset=9 | lzma.PRESET_EXTREME))
print({n: (v[0], len(v[1]), sum(u[1] is None for u in v[1]), round(sum(u[2] for u in v[1]), 2))
       for n, v in result["tests"].items()}, Path(out).stat().st_size, file=sys.stderr)
for n, units in result.get("split", {}).items():
    print("split", n, len(units), "unstable", sum(u[1] is None for u in units),
          "cpu", round(sum(u[2] for u in units), 2), file=sys.stderr)
