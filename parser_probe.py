"""Check the node-limit regression on a smaller input in a fresh interpreter."""
import sys
import unittest
from pathlib import Path

from import_check import PatchedModule


def exercise():
    from sqlglot import exp, parse_one
    from sqlglot.errors import ParseError

    check = unittest.TestCase()
    check.assertIsInstance(parse_one("SELECT 1", max_nodes=1_000_000), exp.Select)
    cols = [f"col{i}" for i in range(500)]
    check.assertIsInstance(parse_one(f"SELECT {','.join(cols)} FROM t", max_nodes=1_000_000), exp.Select)
    expr = "x" + "=x" * 100
    sql = "SELECT " + ",".join([expr] * 100)
    for limit, message in [(10_000, "Maximum number of AST nodes"), (500, "(500)")]:
        with check.assertRaises(ParseError) as ctx:
            parse_one(sql, max_nodes=limit)
        check.assertIn(message, str(ctx.exception))
    with check.assertRaises(ParseError):
        parse_one("SELECT " + ",".join(f"x={i}" for i in range(100)), max_nodes=5)
    check.assertIsInstance(parse_one("SELECT 1, 2, 3"), exp.Select)


if __name__ == "__main__":
    root, path = sys.argv[1:]
    if path.endswith("/__init__.py") or not path.startswith("sqlglot/"):
        raise ValueError("unsupported module")
    module = path[:-3].replace("/", ".")
    loader = PatchedModule(module, str(Path(root) / path), sys.stdin.buffer.read())
    sys.meta_path.insert(0, loader)
    sys.path.insert(0, root)
    exercise()
    if module not in sys.modules or sys.modules[module].__loader__ is not loader:
        raise RuntimeError("patched module was not loaded")
