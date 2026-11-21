"""Index clean source files that reference checkout metadata."""
import ast
from collections import defaultdict
from importlib.util import resolve_name
import json
from pathlib import Path
import sys
import zlib

from impact import Impact
from observation_file import build
from refine import Details

def load(path):
    return json.loads(zlib.decompress(path.read_bytes()))


def importer_masks(trees, tests):
    modules = {path[:-3].replace("/", ".").removesuffix(".__init__"): path for path in trees}
    names = {name.casefold(): name for name in modules}
    reverse = defaultdict(set)
    for module, path in modules.items():
        package = module if path.endswith("/__init__.py") else module.rpartition(".")[0]
        targets = {package}
        for node in ast.walk(trees[path]):
            if isinstance(node, ast.Import):
                targets.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                name = node.module or ""
                if node.level:
                    name = resolve_name("." * node.level + name, package)
                targets.add(name)
                targets.update(name + "." + alias.name for alias in node.names if alias.name != "*")
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                if node.value.startswith(("sqlglot.", "tests.")):
                    targets.add(node.value)
        for target in targets:
            parts = target.split(".")
            for index in range(1, len(parts) + 1):
                name = names.get(".".join(parts[:index]).casefold())
                if name is not None and name != module:
                    reverse[modules[name]].add(path)
    result = {}
    for path in trees:
        if not path.startswith("sqlglot/"):
            continue
        seen, pending, mask = {path}, [path], 0
        while pending:
            source = pending.pop()
            mask |= tests.get(source, 0)
            fresh = reverse[source] - seen
            pending.extend(fresh)
            seen.update(fresh)
        result["importers:" + path] = ((None, mask),)
    return result


def main(base, directory):
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
    trees = {}
    metadata_reads = {"__annotations__", "__doc__", "co_consts", "get_annotations",
                      "get_type_hints", "getdoc", "getcomments", "getsource",
                      "signature", "DocTestSuite", "DocTestFinder"}
    for source_dir in ("sqlglot", "tests"):
        for source in sorted((base / source_dir).rglob("*.py")):
            name = source.relative_to(base).as_posix()
            tree = ast.parse(source.read_bytes(), filename=name)
            trees[name] = tree
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
    profile = load(directory / "base.z")
    details = Details(load(directory / "details.z"), profile["ids"], base)
    impact = Impact(base, profile, details)

    def readers(paths):
        mask = 0
        for path in paths:
            mask |= (details.imports.get(path, 0) | details.test_files.get(path, 0)
                     | impact.visitors.get(path, 0))
        return mask

    observed = 0
    for path, numbers in introspection.items():
        if path not in details.lines:
            observed |= readers([path])
            continue
        observed |= impact.files.get(path, {}).get(0, 0)
        lines = details.lines[path]
        for number in numbers:
            observed |= lines.get(number, 0)
    records = {"file:" + p: ((None, readers(paths)),) for p, paths in index.items()}
    records["discover"] = ((None, readers(discovery)),)
    records["introspect"] = ((None, observed),)
    records.update(importer_masks(trees, details.test_files))
    build(directory / "project_readers.index", records)


if __name__ == "__main__":
    main(*(Path(arg) for arg in sys.argv[1:]))
