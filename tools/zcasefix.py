"""Add short audited fixture tests to zcase.xz as fixture-only records.

zcasemap.py keeps tests of at least 0.1 recorded seconds. A map built with no minimum also
holds the short optimizer and transpile fixture tests; for those listed in
zstage.FIXTURE_CONSUMERS this copies their cases and CPU (without code or line owners), so the
stage replays them only for commits that edit SQL fixtures.

usage: zcasefix.py ZCASE_XZ ZCASE_ALL_XZ OUT
"""

import lzma
import marshal
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from zstage import FIXTURE_CONSUMERS  # noqa: E402

base_path, full_path, out_path = sys.argv[1:4]
base = marshal.loads(lzma.decompress(open(base_path, "rb").read()))
full = marshal.loads(lzma.decompress(open(full_path, "rb").read()))
assert base["ids"] == full["ids"]
for n, record in full["tests"].items():
    if n in base["tests"] or full["ids"][n] not in FIXTURE_CONSUMERS:
        continue
    base["tests"][n] = {"cases": record["cases"], "cpu": record["cpu"],
                        "outside_cpu": record["outside_cpu"], "codes": {}, "lines": {},
                        "bad_codes": [], "bad_lines": [], "fixture_only": True}
    print("added", full["ids"][n], file=sys.stderr)
with open(out_path, "wb") as f:
    f.write(lzma.compress(marshal.dumps(base), preset=9 | lzma.PRESET_EXTREME))
