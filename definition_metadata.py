"""Recognize edits that change Python's introspection metadata only."""

import ast
from functools import lru_cache

from dependencies import name_bits


PACKAGE_FIELDS = {"__doc__", "__file__", "__name__", "__package__", "__path__", "__spec__"}


def has_future_annotations(tree):
    return any(isinstance(node, ast.ImportFrom) and node.module == "__future__"
               and any(item.name == "annotations" for item in node.names) for node in tree.body)


def identity_decorator(node):
    if not isinstance(node, ast.Lambda):
        return False
    args = node.args
    positional = args.posonlyargs + args.args
    return (len(positional) == 1 and args.vararg is None and not args.kwonlyargs
            and args.kwarg is None and not args.defaults and isinstance(node.body, ast.Name)
            and node.body.id == positional[0].arg)


class Normalize(ast.NodeTransformer):
    def __init__(self, package, identifiers, annotations):
        self.package = package
        self.identifiers = identifiers
        self.annotations = annotations

    def visit_Module(self, node):
        if self.package:
            retained = []
            for line in node.body:
                if not self.self_alias(line):
                    retained.append(line)
            node.body = retained
        return self.visit_scoped(node)

    def self_alias(self, node):
        return (isinstance(node, ast.ImportFrom) and node.level == 0 and node.module == self.package
                and len(node.names) == 1 and node.names[0].name in PACKAGE_FIELDS
                and node.names[0].asname and not node.names[0].asname.startswith("_")
                and not all(self.identifiers[i // 8] & (1 << (i % 8))
                            for i in name_bits(node.names[0].asname.encode(), len(self.identifiers))))

    def visit_scoped(self, node):
        if ast.get_docstring(node, clean=False) is not None:
            node.body = node.body[1:]
        return self.generic_visit(node)

    def visit_ClassDef(self, node):
        if any(identity_decorator(d) for d in node.decorator_list):
            node.decorator_list = [d for d in node.decorator_list if not identity_decorator(d)]
        return self.visit_scoped(node)

    def visit_FunctionDef(self, node):
        self.visit_ClassDef(node)
        if self.annotations:
            args = node.args
            for arg in [*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg]:
                if arg is not None and arg.annotation is not None:
                    arg.annotation = ast.Constant(None)
            if node.returns is not None:
                    node.returns = ast.Constant(None)
        return node

    visit_AsyncFunctionDef = visit_FunctionDef


@lru_cache(maxsize=128)
def normalized(source, package, identifiers, annotations):
    tree = ast.parse(source)
    return ast.dump(Normalize(package, identifiers, annotations).visit(tree))


def equivalent(path, before, after, old, new, identifiers):
    annotations = has_future_annotations(old) and has_future_annotations(new)
    package = path.removesuffix("/__init__.py").replace("/", ".") if path.endswith("/__init__.py") else ""
    if not identifiers:
        package = ""
    return normalized(before, package, identifiers, annotations) == normalized(after, package, identifiers, annotations)
