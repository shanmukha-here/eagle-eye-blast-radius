"""Measure selection cost by a general diff feature after normal selection."""
import ast
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys

from impact import UnknownChange, patched, split_diff

HEADERS = re.compile(r"^diff --git a/([\w./-]+) b/([\w./-]+)$")
CORE = {"parser.py", "tokenizer.py", "tokens.py", "generator.py"}


def paths(text):
    lines = [line for line in text.splitlines() if line.startswith("diff --git ")]
    files = [HEADERS.fullmatch(line) for line in lines]
    if not files or not all(files):
        return ()
    return tuple(sorted({path for match in files for path in match.groups()}))


class StripDocstrings(ast.NodeTransformer):
    def generic_visit(self, node):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            if ast.get_docstring(node, clean=False) is not None:
                node.body = node.body[1:]
        return super().generic_visit(node)


def without_docstrings(source):
    return ast.dump(StripDocstrings().visit(ast.parse(source)))


def matches(mode, text, base):
    files = paths(text)
    if not files:
        return False
    python = [path for path in files if path.endswith((".py", ".pyi"))]
    if mode == "b":
        return not python
    if mode == "c":
        return any(path.startswith(("sqlglot/optimizer/", "sqlglot/executor/")) for path in python)
    if mode == "d":
        return any(path.startswith(("sqlglot/parsers/", "sqlglot/generators/")) or
                   path.removeprefix("sqlglot/") in CORE and path.startswith("sqlglot/")
                   for path in python)
    if mode == "a" and len(python) == len(files):
        try:
            for path, hunks in split_diff(text).items():
                old = (base / path).read_text()
                if without_docstrings(old) != without_docstrings(patched(old, hunks)):
                    return False
        except (OSError, SyntaxError, UnicodeError, UnknownChange):
            return False
        return True
    return False


def main():
    mode = os.getenv("PROBE_EMPTY_CLASS", "")
    selector = str(Path(__file__).with_name("selector.py"))
    command = [sys.executable, selector]
    if not mode:
        os.execv(sys.executable, command)
    if mode not in {"a", "b", "c", "d"}:
        raise ValueError("Unknown PROBE_EMPTY_CLASS")
    result = subprocess.run(command, stdout=subprocess.PIPE, check=True)
    for raw in io.BytesIO(result.stdout):
        line = json.loads(raw)
        patch = Path("/data/commits", line["commit"] + ".patch")
        try:
            matching = matches(mode, patch.read_text(), Path("/base"))
        except (OSError, UnicodeError):
            matching = False
        if matching:
            print(json.dumps({"commit": line["commit"], "workers": []}, separators=(",", ":")), flush=True)
        else:
            sys.stdout.buffer.write(raw)
            sys.stdout.buffer.flush()


if __name__ == "__main__":
    main()
