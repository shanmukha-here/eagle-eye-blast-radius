"""Which tests read each non-Python file of the pinned checkout.

Fork traces record the files every test opened. Files opened while collecting (importing the
test modules) are listed apart: their content reaches every later test through module state.

usage: zfix.py ROOT TESTS_JSON OUT FORK_DIR [FORK_DIR...]
"""

import glob
import json
import lzma
import marshal
import os
import sys

ROOT, MANIFEST, OUT = map(os.path.abspath, sys.argv[1:4])
dirs = sys.argv[4:]
ids = [t["id"] for t in json.load(open(MANIFEST))["tests"]]
number = {t: i for i, t in enumerate(ids)}
opens = {}
seen = set()
for d in dirs:
    for path in glob.glob(os.path.join(d, "shard-*.marshal")):
        with open(path, "rb") as f:
            records = marshal.load(f)
        for test_id, rec in records.items():
            if not isinstance(rec, dict):
                continue
            seen.add(test_id)
            for p in rec["opens"]:
                if not p.endswith(".py"):
                    opens[p] = opens.get(p, 0) | 1 << number[test_id]
if seen != set(ids):
    raise SystemExit("traces do not cover every test")

os.chdir(ROOT)
sys.path.insert(0, ROOT)
sys.path.insert(1, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PREFIX = ROOT + os.sep
collect = set()


def audit(event, args):
    if event == "open" and isinstance(args[0], str):
        path = os.path.abspath(args[0])
        if path.startswith(PREFIX):
            collect.add(path[len(PREFIX):])


sys.addaudithook(audit)
import zrun  # noqa: E402

zrun.load(ROOT, ids)
data = {"opens": opens, "collect": sorted(p for p in collect if not p.endswith((".py", ".pyc")))}
with open(OUT, "wb") as f:
    f.write(lzma.compress(marshal.dumps(data), preset=9 | lzma.PRESET_EXTREME))
print(len(opens), "files", len(data["collect"]), "read at collection:", data["collect"][:20], file=sys.stderr)
