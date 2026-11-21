"""Index clean source files that reference checkout metadata."""
import ast
import json
from pathlib import Path
import sys
import zlib

from impact import Impact
from observation_file import build
from refine import Details

def load(path):
    return json.loads(zlib.decompress(path.read_bytes()))


def key(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def standard_super_hook(node):
    return (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name) and node.value.func.id == "super"
            and not node.value.args and not node.value.keywords)


def class_doc(node, parents):
    """Recognize doc access on the class supplied by Python's subclass hook."""
    parent = parents.get(node)
    if isinstance(node, ast.Attribute):
        receiver = node.value
    elif (isinstance(parent, ast.Call) and isinstance(parent.func, ast.Name)
          and parent.func.id in {"getattr", "setattr"} and len(parent.args) >= 2 and parent.args[1] is node):
        receiver = parent.args[0]
    else:
        return False
    if not isinstance(receiver, ast.Name):
        return False
    current = parent
    while current is not None and not isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
        current = parents.get(current)
    if not (isinstance(current, ast.FunctionDef) and current.name == "__init_subclass__"
            and isinstance(parents.get(current), ast.ClassDef)):
        return False
    args = current.args.posonlyargs + current.args.args
    if not args or args[0].arg != receiver.id:
        return False
    if any(not isinstance(item, ast.Name) or item.id != "classmethod" for item in current.decorator_list):
        return False
    return not any(
        isinstance(item, ast.Name) and item.id == receiver.id and isinstance(item.ctx, (ast.Store, ast.Del))
        or isinstance(item, (ast.Nonlocal, ast.Global)) and receiver.id in item.names
        or isinstance(item, (ast.ExceptHandler, ast.MatchAs, ast.MatchStar)) and item.name == receiver.id
        for item in ast.walk(current)
    )


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
    source_introspection = {}
    doc_introspection = {}
    class_doc_introspection = {}
    annotation_introspection = {}
    manual_subclass_hook = False
    metadata_reads = {"__annotations__", "__doc__", "co_consts", "get_annotations",
                      "get_type_hints", "getdoc", "getcomments", "getsource",
                      "signature", "DocTestSuite", "DocTestFinder"}
    source_reads = {"co_consts", "co_firstlineno", "co_linetable", "co_lnotab", "co_lines", "co_positions",
                    "findsource", "getcomments", "getframeinfo", "getsource", "getsourcelines", "linecache",
                    "settrace", "f_lineno", "f_trace", "tb_lineno", "extract_stack", "format_stack",
                    "get_instructions", "disassemble"}
    doc_reads = {"__doc__", "getdoc", "DocTestSuite", "DocTestFinder", "find_doctest"}
    annotation_reads = {"__annotations__", "__annotate__", "get_annotations", "get_type_hints",
                        "signature", "annotationlib"}
    for source_dir in ("sqlglot", "tests"):
        for source in sorted((base / source_dir).rglob("*.py")):
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
            physical = sorted({n.lineno for n in nodes if hasattr(n, "lineno") and (
                isinstance(n, ast.Name) and n.id in source_reads
                or isinstance(n, ast.Attribute) and n.attr in source_reads
                or isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value in source_reads
            )})
            if physical:
                source_introspection[name] = physical
            parents = {child: node for node in nodes for child in ast.iter_child_nodes(node)}
            builtins_used = {"getattr", "setattr", "super"}
            shadowed = any(
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name in builtins_used
                or isinstance(node, ast.Name) and not isinstance(node.ctx, ast.Load) and node.id in builtins_used
                or isinstance(node, ast.arg) and node.arg in builtins_used
                or isinstance(node, ast.alias) and (node.asname or node.name.split(".", 1)[0]) in builtins_used
                for node in nodes
            )
            for node in nodes:
                word = key(node)
                if word == "__init_subclass__" and (shadowed or not standard_super_hook(node)):
                    manual_subclass_hook = True
                if word in doc_reads:
                    class_only = word == "__doc__" and not shadowed and class_doc(node, parents)
                    locations = class_doc_introspection if class_only else doc_introspection
                    locations.setdefault(name, []).append(node.lineno)
                if word in annotation_reads:
                    annotation_introspection.setdefault(name, []).append(node.lineno)
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

    def observations(locations):
        observed = 0
        for path, numbers in locations.items():
            if path not in details.lines:
                observed |= readers([path])
                continue
            observed |= impact.files.get(path, {}).get(0, 0)
            lines = details.lines[path]
            for number in numbers:
                observed |= lines.get(number, 0)
        return observed
    records = {"file:" + p: ((None, readers(paths)),) for p, paths in index.items()}
    records["discover"] = ((None, readers(discovery)),)
    records["introspect"] = ((None, observations(introspection)),)
    records["source_layout"] = ((None, observations(source_introspection)),)
    function_docs = observations(doc_introspection)
    if manual_subclass_hook:
        function_docs |= observations(class_doc_introspection)
    records["function_docs"] = ((None, function_docs),)
    records["annotations"] = ((None, observations(annotation_introspection)),)
    build(directory / "project_readers.index", records)


if __name__ == "__main__":
    main(*(Path(arg) for arg in sys.argv[1:]))
