"""Build line, dialect and import indices from clean-base observations only."""

import argparse
import ast
import json
import zlib
from collections import defaultdict
from functools import lru_cache
from importlib.util import resolve_name
from pathlib import Path


def module(path):
    return path.removesuffix(".py").removesuffix("/__init__").replace("/", ".")


def closures(modules, edges):
    reverse = defaultdict(set)
    for source, deps in edges.items():
        for dep in deps:
            reverse[dep].add(source)

    def parents(name):
        found = {name}
        todo = [name]
        while todo:
            extra = reverse[todo.pop()] - found
            todo.extend(extra)
            found.update(extra)
        return found

    return {name: parents(name) for name in modules}


def build(directory, type_directory, operand_directory, root, base):
    docs = [
        json.loads(zlib.decompress(p.read_bytes()))
        for p in sorted(directory.glob("detail-*.z"))
    ]
    ids = base["ids"]
    assert len(docs) > 1 and [sorted(items) for items, _, _ in docs if items] == [ids]
    full = (1 << len(ids)) - 1
    masks = {"*": full}
    for i, name in enumerate(ids):
        masks[name] = 1 << i
        f = name.split("::", 1)[0]
        masks[f] = masks.get(f, 0) | (1 << i)
    receiver = defaultdict(int)
    type_sessions = []
    for p in type_directory.glob("types-*.z"):
        items, contexts = json.loads(zlib.decompress(p.read_bytes()))
        if items:
            type_sessions.append(sorted(items))
        for tag, names in contexts.items():
            for name in names:
                receiver[name] |= masks[tag]
    assert type_sessions == [ids]
    operand_masks = defaultdict(lambda: defaultdict(int))
    operand_sessions = []
    for p in operand_directory.glob("operands-*.z"):
        tests, contexts = json.loads(zlib.decompress(p.read_bytes()))
        if tests:
            operand_sessions.append(sorted(tests))
        for tag, values in contexts.items():
            for key, actual_type in values:
                # Shared setup can widen impact, but cannot certify an individual test.
                kind = actual_type if "::" in tag else "unresolved"
                operand_masks[key][kind] |= masks[tag]
    assert operand_sessions == [ids]
    lines = defaultdict(dict)
    for _, contexts, files in docs:
        owners = [None] * len(contexts)
        for key, i in contexts.items():
            owners[i] = masks[key]

        @lru_cache(maxsize=None)
        def translate(bits):
            out = 0
            while bits:
                low = bits & -bits
                out |= owners[low.bit_length() - 1]
                if out == full:
                    break
                bits ^= low
            return out

        for f, records in files.items():
            for k, word in records.items():
                lines[f][k] = lines[f].get(k, 0) | translate(int(word, 16))
    dialects = lines.pop("@dialect")
    paths = [
        p.relative_to(root).as_posix()
        for folder in ("sqlglot", "tests")
        for p in (root / folder).rglob("*.py")
    ]
    modules = {module(f): f for f in paths}
    edges = defaultdict(set)
    observers = 0
    leaf = {
        m
        for m in modules
        if len(m.split(".")) == 3
        and m.split(".")[1] in ("dialects", "parsers", "generators", "typing")
        and m.startswith("sqlglot.")
        and m != "sqlglot.dialects.dialect"
    }
    uses = defaultdict(int)
    external = defaultdict(int)
    exported_classes = defaultdict(list)
    position_attrs = {
        "co_firstlineno",
        "co_lines",
        "co_linetable",
        "co_positions",
        "f_lineno",
        "tb_lineno",
        "getsourcelines",
        "getsource",
        "extract_tb",
        "format_exc",
    }
    for name, f in modules.items():
        tree = ast.parse((root / f).read_bytes())

        def collect_classes(nodes, prefix=()):
            for cls in nodes:
                if isinstance(cls, ast.ClassDef):
                    qualified = (*prefix, cls.name)
                    exported_classes[".".join(qualified)].append(
                        f + ":" + ".".join(qualified)
                    )
                    collect_classes(cls.body, qualified)

        collect_classes(tree.body)
        for fn in ast.walk(tree):
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for stmt in fn.body:
                    for expr in ast.walk(stmt):
                        if isinstance(expr, (ast.Name, ast.Attribute)):
                            symbol = (
                                expr.id if isinstance(expr, ast.Name) else expr.attr
                            )
                            if symbol[:1].isupper():
                                mask = lines[f].get(str(expr.lineno), 0)
                                uses[symbol] |= mask
                                part = expr
                                while isinstance(part, ast.Attribute):
                                    part = part.value
                                    prefix = (
                                        part.attr
                                        if isinstance(part, ast.Attribute)
                                        else getattr(part, "id", "")
                                    )
                                    if not prefix or not prefix[:1].isupper():
                                        break
                                    symbol = prefix + "." + symbol
                                    uses[symbol] |= mask
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in position_attrs:
                observers |= lines[f].get(str(node.lineno), 0)
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                path = "." * node.level + (node.module or "")
                if node.level:
                    path = resolve_name(
                        path,
                        name if f.endswith("__init__.py") else name.rpartition(".")[0],
                    )
                names = [path] + [path + "." + a.name for a in node.names]
            else:
                continue
            dependencies = {n for n in names if n in modules}
            edges[name].update(dependencies)
            if name not in leaf:
                for n in dependencies & leaf:
                    external[n] |= masks.get(f, 0) | lines[f].get(str(node.lineno), 0)
    reverse = closures(modules, edges)
    family_reverse = closures(
        leaf, {n: deps & leaf for n, deps in edges.items() if n in leaf}
    )
    imports, families = {}, {}
    for name, affected in reverse.items():
        readers = family = 0
        for dep in affected:
            f = modules[dep]
            readers |= masks.get(f, 0)
        for dep in family_reverse.get(name, ()):
            family |= dialects.get(modules[dep], 0) | external[dep]
        imports[modules[name]] = readers
        if family:
            families[modules[name]] = family
    # Deduplicate recurring bitsets before storing the line index.
    pool = [0]
    indices = {0: 0}

    def index(bits):
        if bits not in indices:
            indices[bits] = len(pool)
            pool.append(bits)
        return indices[bits]

    records = {
        f: {k: index(bits) for k, bits in sorted(v.items())}
        for f, v in sorted(lines.items())
        if v
    }
    return {
        "operands": {
            key: {kind: index(bits) for kind, bits in sorted(types.items())}
            for key, types in sorted(operand_masks.items())
        },
        "types": {key: index(mask) for key, mask in sorted(receiver.items())},
        "references": {
            key: index(uses[symbol])
            for symbol in sorted(exported_classes)
            for key in sorted(exported_classes[symbol])
        },
        "lines": records,
        "imports": {f: index(b) for f, b in sorted(imports.items()) if b},
        "dialects": {f: index(b) for f, b in sorted(families.items())},
        "positions": index(observers),
        "bits": [format(b, "x") for b in pool],
    }


if __name__ == "__main__":
    a = argparse.ArgumentParser(description=__doc__)
    for name in ("traces", "types", "operands", "root", "base", "out"):
        a.add_argument(name, type=Path)
    args = a.parse_args()
    profile = json.loads(zlib.decompress(args.base.read_bytes()))
    data = build(args.traces, args.types, args.operands, args.root, profile)
    args.out.write_bytes(
        zlib.compress(json.dumps(data, separators=(",", ":")).encode(), 9)
    )
