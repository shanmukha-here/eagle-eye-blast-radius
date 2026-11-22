"""Build the value maps for module and class statement edits from the pinned source.

zval.xz: the digest of every module global and class attribute after importing all of
sqlglot (zvalues.snapshot). Keys whose ordered digest differs between separate interpreters
(dicts filled from sets of classes, whose order follows addresses) are compared unordered.
Function-like values list the code they run.

zattr.xz: from zattr.py shards, the tests that read each class attribute and module attribute,
the attributes read during collection, the tracked class attributes, and for each module
global the code objects of its file that name it.

usage: zvalmap.py ROOT TESTS_JSON OUT_DIR ATTR_DIR...
       zvalmap.py --child ROOT   (writes one snapshot to stdout)
"""

import glob
import lzma
import marshal
import os
import subprocess
import sys
import types

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
NEWLOCALS = 0x2


def child(root):
    # Shift every later address so set orders that follow addresses show up across runs.
    n = int(os.environ.get("ZVAL_PERTURB", "0"))
    keep = [bytearray(16 * (1 + i % 7)) for i in range(n)] + [object() for _ in range(n % 13)]
    sys.path.insert(0, root)
    import zrun
    import zvalues
    zrun.preload_all(root)
    ordered, opaque = zvalues.snapshot(root)
    loose, _ = zvalues.snapshot(root, False)
    codes = {}
    for key, v in zvalues.items(root):
        found = zvalues.code_keys(root, v)
        if found is not None:
            codes[key] = found
    sys.stdout.buffer.write(marshal.dumps((ordered, loose, codes, opaque)))
    del keep


def pack(obj):
    return lzma.compress(marshal.dumps(obj), preset=9 | lzma.PRESET_EXTREME)


def main():
    import json
    root, manifest, out = map(os.path.abspath, sys.argv[1:4])
    ids = [t["id"] for t in json.load(open(manifest))["tests"]]
    env = dict(os.environ, PYTHONHASHSEED="0", PYTHONDONTWRITEBYTECODE="1")
    runs = []
    for n in (0, 1, 3, 7, 19, 50, 123, 400, 1001, 2500, 6007, 15000):
        data = subprocess.run([sys.executable, __file__, "--child", root], env=dict(env, ZVAL_PERTURB=str(n)), cwd=root,
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=True).stdout
        runs.append(marshal.loads(data))
    ordered, loose, codes, opaque = runs[0]
    if opaque:
        raise SystemExit("opaque values: %s" % opaque)
    unstable = {k for r in runs[1:] for k in ordered if r[0].get(k) != ordered[k]}
    bad = {k for r in runs[1:] for k in unstable if r[1].get(k) != loose[k]}
    if bad or any(set(r[0]) != set(ordered) for r in runs):
        raise SystemExit("unstable values: %s" % sorted(bad)[:10])
    keys = sorted(ordered)
    zval = {"keys": keys, "digests": [ordered[k] for k in keys],
            "loose_keys": sorted(unstable), "loose": [loose[k] for k in sorted(unstable)],
            "codes": codes, "python": __import__("importlib.util").util.MAGIC_NUMBER}
    with open(os.path.join(out, "zval.xz"), "wb") as f:
        f.write(pack(zval))
    print("values", len(keys), "loose", len(unstable), "codes", len(codes), file=sys.stderr)
    # A no-op patch must report no changes through the runtime child.
    for n in (0, 2, 11, 333, 4097):
        raw = subprocess.run([sys.executable, "-B", os.path.join(HERE, "zvalues.py")],
                             input=marshal.dumps((root, os.path.join(out, "zval.xz"), {})),
                             env=dict(env, ZVAL_PERTURB=str(n)), cwd=root, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, check=True).stdout
        result = marshal.loads(raw)
        print("no-op", n, result if result[0] == "error" else [len(x) for x in result], file=sys.stderr)
        if result[0] == "error" or any(result):
            raise SystemExit("no-op diff not empty: %s" % (result,))

    # Reads per test, united over the traced hash seeds.
    reads, collect, tracked, failed = {}, set(), None, set()
    for directory in sys.argv[4:]:
        shards = sorted(glob.glob(os.path.join(directory, "shard-*.marshal")))
        records = {}
        for path in shards:
            with open(path, "rb") as f:
                r = marshal.load(f)
            collect.update(map(tuple, r["<collect>"]))
            if r["<tracked>"] is not None:
                tracked = set(map(tuple, r["<tracked>"]))
            records.update({k: v for k, v in r.items() if not k.startswith("<")})
        for i, test in enumerate(ids):
            rec = records.get(test)
            if not isinstance(rec, dict) or rec["failed"]:
                failed.add(i)
                continue
            for key in rec["reads"]:
                key = tuple(key)
                reads[key] = reads.get(key, 0) | (1 << i)
    # Module globals named by code objects of their own file.
    globals_ = {}
    module_names = {}
    for key in keys:
        if key[0] == "m":
            module_names.setdefault(key[1], set()).add(key[2])
    for rel, names in module_names.items():
        with open(os.path.join(root, rel), encoding="utf-8", newline="") as f:
            code = compile(f.read(), os.path.join(root, rel), "exec", dont_inherit=True)
        stack = [code]
        while stack:
            c = stack.pop()
            stack.extend(k for k in c.co_consts if isinstance(k, types.CodeType))
            if c.co_qualname == "<module>":
                continue
            entry = ((rel, c.co_qualname, c.co_firstlineno), bool(c.co_flags & NEWLOCALS))
            for name in set(c.co_names) & names:
                globals_.setdefault((rel, name), []).append(entry)
    # Code started while importing sqlglot, and while collecting tests (zphase.py outputs).
    imported, collected = set(), set()
    for path in glob.glob(os.path.join(out, "phase-*.marshal")):
        with open(path, "rb") as f:
            ph = marshal.load(f)
        imported.update(map(tuple, ph["import"]))
        collected.update(map(tuple, ph["collect"]))
    rkeys = sorted(reads)
    zattr = {"ids": ids, "keys": rkeys, "masks": [reads[k] for k in rkeys], "collect": sorted(collect),
             "tracked": sorted(tracked), "failed": sum(1 << i for i in failed), "globals": globals_,
             "import_codes": sorted(imported), "collect_codes": sorted(collected)}
    with open(os.path.join(out, "zattr.xz"), "wb") as f:
        f.write(pack(zattr))
    print("read keys", len(rkeys), "collect", len(collect), "tracked", len(tracked), "failed", len(failed),
          "globals", len(globals_), file=sys.stderr)


if __name__ == "__main__":
    if sys.argv[1] == "--child":
        child(os.path.abspath(sys.argv[2]))
    else:
        main()
