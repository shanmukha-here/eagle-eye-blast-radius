"""Index clean source files that reference checkout metadata."""
import ast
import json
from pathlib import Path
import sys


def main(base, target):
    paths = set()
    for glob in ("*.md", "docs/**/*.md"):
        paths.update(path.relative_to(base).as_posix() for path in base.glob(glob) if path.is_file())
    paths.update({"pyproject.toml", "setup.cfg"})
    index = {path: set() for path in sorted(paths)}
    metadata = {"pyproject.toml", "setup.cfg"}
    setup_consumers = {"build_ext", "Distribution", "setuptools", "setuptools_scm",
                       "find_config_files", "parse_config_files"}
    discovery = set()
    introspection = {}
    metadata_reads = {"__annotations__", "__doc__", "co_consts", "get_annotations",
                      "get_type_hints", "getdoc", "getcomments", "getsource",
                      "signature", "DocTestSuite", "DocTestFinder"}
    for directory in ("sqlglot", "tests"):
        for source in sorted((base / directory).rglob("*.py")):
            name = source.relative_to(base).as_posix()
            tree = ast.parse(source.read_bytes(), filename=name)
            nodes = list(ast.walk(tree))
            docstrings = {n.body[0].value for n in nodes if isinstance(n, (ast.Module, ast.ClassDef,
                            ast.FunctionDef, ast.AsyncFunctionDef)) and ast.get_docstring(n) is not None}
            strings = [n.value for n in nodes if isinstance(n, ast.Constant)
                       and isinstance(n.value, str) and n not in docstrings]
            identifiers = set().union(
                (n.id for n in nodes if isinstance(n, ast.Name)),
                (n.attr for n in nodes if isinstance(n, ast.Attribute)),
                (n.name for n in nodes if isinstance(n, ast.alias)),
            )
            if identifiers.intersection({"walk_packages", "iter_modules"}):
                discovery.add(name)
            lines = sorted({n.lineno for n in nodes if hasattr(n, "lineno") and (
                isinstance(n, ast.Name) and n.id in metadata_reads
                or isinstance(n, ast.Attribute) and n.attr in metadata_reads
                or isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value in metadata_reads
            )})
            if lines:
                introspection[name] = lines
            for path, readers in index.items():
                basename = path.rpartition("/")[2]
                if any(basename in value for value in strings) or (
                    path in metadata and setup_consumers.intersection(identifiers)
                ):
                    readers.add(name)
    result = {"files": {p: sorted(readers) for p, readers in index.items()},
              "directories": sorted(discovery), "introspection": introspection}
    target.write_text(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main(*(Path(arg) for arg in sys.argv[1:]))
