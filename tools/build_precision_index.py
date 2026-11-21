"""Combine base pytest parent and worker traces without using patch answers."""
from collections import Counter, defaultdict
from functools import lru_cache
import json
from pathlib import Path
import sys
from types import CodeType
import zlib

from precision_index import build


def observations(directory, profile, replay, root):
    ids = profile["ids"]
    owners = defaultdict(int)
    for i, name in enumerate(ids):
        bit = 1 << i
        owners[name] |= bit
        owners[name.partition("::")[0]] |= bit
    full = (1 << len(ids)) - 1
    owners["*"] = full
    queries = {row[0]: 1 << i for i, row in enumerate(replay["cases"])}
    test = replay["test"]
    rows = defaultdict(int)
    indexed = set()
    sessions = []

    @lru_cache(maxsize=None)
    def locations(path):
        out = {}
        pending = [compile((root / path).read_bytes(), str(root / path), "exec", dont_inherit=True)]
        while pending:
            code = pending.pop()
            pending.extend(value for value in code.co_consts if isinstance(value, CodeType))
            name = f"{code.co_firstlineno}:{code.co_qualname}"
            out[name] = {offset: line for start, end, line in code.co_lines()
                         for offset in range(start, end, 2) if line is not None}
        return out

    for raw in sorted(directory.glob("precision-*.z")):
        data = json.loads(zlib.decompress(raw.read_bytes()))
        sessions.extend(data["items"])
        for name in data["tables"]:
            rows["T:" + name] = 1
        for owner, events in data["records"].items():
            parent, sep, query = owner.partition("@query:")
            context = owners[parent]
            query_bit = queries.get(query, 0)
            if sep and not query_bit:
                raise ValueError("Unexpected clean executor query")
            if query_bit:
                indexed.add(query)
            for kind, path, name, key in events:
                if kind == "L":
                    suffix = f"{path}:{name}"
                elif kind == "F":
                    suffix = f"{path}:{name}:{key}"
                elif kind == "B":
                    suffix = f"{path}:{name}:{key}"
                    filename, _, scope = path.partition(":")
                    bytecode = locations(filename).get(scope, {})
                    source_line, target_line = bytecode.get(name), bytecode.get(key)
                    if source_line is not None and target_line is not None:
                        rows[f"BL:{filename}:{source_line}"] |= context
                        rows[f"BD:{filename}:{source_line}:{target_line}"] |= context
                elif kind == "K":
                    suffix = f"{path}:{name}"
                else:
                    suffix = path
                rows[f"{kind}:{suffix}"] |= context
                if kind in ("L", "F"):
                    if query_bit:
                        rows[f"Q{kind}:{suffix}"] |= query_bit
                    elif context & owners[test]:
                        rows[f"O{kind}:{suffix}"] |= 1
    if not set(ids) <= set(sessions) or len(sessions) != len(set(sessions)) or indexed != queries.keys():
        raise ValueError(f"Incomplete trace. Tests {len(sessions)}, query cases {len(indexed)}")
    rows["!ready"] = full
    print("precision trace", len(sessions), "tests", len(indexed), "queries",
          dict(Counter(key.partition(":")[0] for key in rows)), flush=True)
    return rows, (len(ids) + 7) // 8


def main(trace, directory, root):
    def load(name):
        return json.loads(zlib.decompress((directory / name).read_bytes()))
    rows, width = observations(trace, load("base.z"), load("replay_index.z"), root)
    output = directory / "precision.index"
    build(output, rows, width)
    print("precision index bytes", output.stat().st_size, flush=True)


if __name__ == "__main__":
    main(*(Path(a) for a in sys.argv[1:]))
