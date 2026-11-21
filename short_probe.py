"""Check complete per-dialect allocation loops or the exact DuckDB query inputs."""
import gc
import hashlib
import json
import pickle
import sys
import tracemalloc
from pathlib import Path
from import_check import PatchedModule


def memory(dialects):
    from sqlglot.dialects import Dialect
    for name in dialects:
        dialect = Dialect.get_or_raise(name)
        for _ in range(100):
            dialect.generator()
        gc.collect()
        tracemalloc.start()
        try:
            before = tracemalloc.get_traced_memory()[0]
            for _ in range(1000):
                dialect.generator()
            gc.collect()
            after = tracemalloc.get_traced_memory()[0]
        finally:
            tracemalloc.stop()
        if after - before >= 1024:
            return False
    return bool(dialects)


def execute():
    from sqlglot import parse_one
    from tests.helpers import load_sql_fixture_pairs

    pairs = list(load_sql_fixture_pairs("optimizer/tpc-h/tpc-h.sql"))
    if len(pairs) != 22 or any(meta.get("execute") for meta, _, _ in pairs):
        return False
    for meta, sql, _ in pairs:
        tree, copy_meta = pickle.loads(pickle.dumps((parse_one(sql), meta)))
        if tree is None or copy_meta != meta:
            return False
    return True


def optimized():
    from sqlglot import transpile
    from tests.helpers import load_sql_fixture_pairs
    expected = json.loads(Path(__file__).with_name("tpch_sql.json").read_text())
    pairs = list(load_sql_fixture_pairs("optimizer/tpc-h/tpc-h.sql"))
    return len(expected) == len(pairs) == 22 and all(
        hashlib.sha256(transpile(text, write="duckdb")[0].encode()).hexdigest() == checksum
        for (_, sql, opt), row in zip(pairs, expected)
        for text, checksum in zip((sql, opt), row, strict=True)
    )


if __name__ == "__main__":
    root, path, kind, *cases = sys.argv[1:]
    module = path[:-3].replace("/", ".")
    loader = PatchedModule(module, str(Path(root) / path), sys.stdin.buffer.read())
    sys.meta_path.insert(0, loader)
    sys.path.insert(0, root)
    okay = {"execute": execute, "optimized": optimized, "memory": lambda: memory(cases)}[kind]()
    sys.exit(0 if okay and module in sys.modules and sys.modules[module].__loader__ is loader else 1)
