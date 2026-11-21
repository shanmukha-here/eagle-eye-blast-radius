"""Combine clean runtime module use with the reverse import graph."""

import ast
from collections import defaultdict
import importlib.util
import json
from pathlib import Path
import sys
import zlib

from precision_index import PrecisionIndex, build
from impact import structure


class Imports(ast.NodeVisitor):
    def __init__(self, package, modules):
        self.package = package
        self.modules = modules
        self.result = set()

    def add(self, name):
        while name:
            if name in self.modules:
                self.result.add(self.modules[name])
            name = name.rpartition(".")[0]

    def visit_Import(self, node):
        for alias in node.names:
            self.add(alias.name)

    def visit_ImportFrom(self, node):
        name = node.module or ""
        if node.level:
            try:
                name = importlib.util.resolve_name("." * node.level + name, self.package)
            except ImportError:
                return
        self.add(name)
        for alias in node.names:
            self.add(f"{name}.{alias.name}")

    def visit_If(self, node):
        kind = node.test
        if (isinstance(kind, ast.Name) and kind.id == "TYPE_CHECKING" or
                isinstance(kind, ast.Attribute) and kind.attr == "TYPE_CHECKING"
                and isinstance(kind.value, ast.Name) and kind.value.id == "typing"):
            for statement in node.orelse:
                self.visit(statement)
        else:
            self.generic_visit(node)


def main(root, out):
    modules = {}
    for directory in ("sqlglot", "tests"):
        for file in sorted((root / directory).rglob("*.py")):
            path = file.relative_to(root).as_posix()
            key = path.removesuffix(".py").removesuffix("/__init__").replace("/", ".")
            modules[key] = path

    def read(name):
        return json.loads(zlib.decompress((out / name).read_bytes()))

    base, details = read("base.z"), read("details.z")
    precision = PrecisionIndex(out / "precision.index")
    if not precision.mask("!import_lines"):
        raise ValueError("Missing clean import-time observations")
    observed = {path:int(mask, 16) for path, mask in base["visitors"].items()}
    pool = [int(mask, 16) for mask in details["bits"]]
    for family in ("imports", "dialects"):
        for path, index in details.get(family, {}).items():
            observed[path] = observed.get(path, 0) | pool[index]
    for i, node in enumerate(base["ids"]):
        path = node.split("::", 1)[0]
        observed[path] = observed.get(path, 0) | (1 << i)

    consumers = defaultdict(set)
    functions = defaultdict(int)
    members = defaultdict(int)
    for name, path in modules.items():
        package = name if path.endswith("/__init__.py") else name.rpartition(".")[0]
        scanner = Imports(package, modules)
        tree = ast.parse((root / path).read_bytes(), path)
        scanner.visit(tree)
        lines = details["lines"].get(path, {})
        is_test = path.startswith("tests/")
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                member = node.attr
            elif isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.isidentifier():
                member = node.value
            else:
                continue
            recorded = members["attr:" + member]
            if is_test:
                recorded |= observed.get(path, 0)
            for line in range(node.lineno, node.end_lineno + 1):
                line_mask = lines.get(str(line))
                if line_mask is not None:
                    recorded |= pool[line_mask]
                recorded |= precision.mask(f"IL:{path}:{line}")
            members["attr:" + member] = recorded
        for imported in scanner.result - {path}:
            consumers[imported].add(path)
        _, declarations = structure(tree)
        scope_masks = base["files"].get(path, {})
        for scope, (line, _, _, _) in declarations.items():
            mask = int(scope_masks.get(str(line), "0"), 16)
            functions["call:" + scope[-2]] |= mask
            functions[f"call:{path}:{'.'.join(scope[:-1])}"] |= mask

    rows = {}
    for path in modules.values():
        queue, seen = [path], {path}
        mask = 0
        while queue:
            current = queue.pop()
            mask |= observed.get(current, 0)
            following = consumers[current] - seen
            seen.update(following)
            queue.extend(following)
        rows[path] = mask
    build(out / "import_closure.index", rows | functions | members | {"!attr_references": 1},
          (len(base["ids"]) + 7) // 8)
    print(f"indexed {len(rows)} transitive module consumers", flush=True)


if __name__ == "__main__":
    main(*(Path(v) for v in sys.argv[1:]))
