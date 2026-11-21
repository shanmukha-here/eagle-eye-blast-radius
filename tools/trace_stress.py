"""Record token predicates as they execute in the original, unscaled parser test."""
import functools
import json
import sys
import unittest
import zlib
from collections import defaultdict
from pathlib import Path

from sqlglot.parser import Parser
from sqlglot.tokens import TokenType
from tests.test_parser import TestParser


def main(output):
    observations = defaultdict(set)
    names = {member: name for name, member in TokenType.__members__.items()}
    if len(names) != len(TokenType.__members__):
        raise ValueError("aliased token kinds require canonicalization")
    original = {}

    def wrapper(method, function):
        @functools.wraps(function)
        def match(self, *args, **kwargs):
            caller = sys._getframe(1)
            if caller.f_globals.get("__name__") == "sqlglot.parser":
                key = f"{caller.f_lineno}:{method}"
                values = [names[self._curr.token_type]]
                if method == "_match_pair":
                    values.append(names[self._next.token_type])
                observations[key].add(tuple(values))
            return function(self, *args, **kwargs)
        return match

    for name in ("_match", "_match_pair"):
        original[name] = getattr(Parser, name)
        setattr(Parser, name, wrapper(name, original[name]))
    try:
        result = unittest.TextTestRunner().run(TestParser("test_max_nodes"))
    finally:
        for name, method in original.items():
            setattr(Parser, name, method)
    if not result.wasSuccessful() or len(observations) < 10:
        raise RuntimeError("incomplete upstream parser trace")
    rows = {"members": sorted(TokenType.__members__),
            "calls": {k: sorted(v) for k, v in sorted(observations.items())}}
    output.write_bytes(zlib.compress(json.dumps(rows, separators=(",", ":")).encode(), 9))


if __name__ == "__main__":
    main(Path(sys.argv[1]))
