"""Add clean definition and import consumers for changes outside function bodies."""

import ast
from functools import lru_cache

from dependencies import name_bits
from impact import UnknownChange, patched, split_diff, syntax
from precision_index import PrecisionIndex
from receiver_edits import classes, plain_method


FUNCTION = (ast.FunctionDef, ast.AsyncFunctionDef)


def declaration_atoms(tree, owner="", enum=False):
    """Describe executed declaration syntax without comparing function bodies."""
    result = {}
    other = 0
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            qualified = f"{owner}.{node.name}" if owner else node.name
            fields = (*node.bases, *node.keywords, *node.decorator_list, *node.type_params)
            result[(owner, node.name, "class")] = tuple(map(ast.dump, fields))
            is_enum = any((isinstance(base, ast.Name) and base.id.endswith("Enum")) or
                          (isinstance(base, ast.Attribute) and base.attr.endswith("Enum")) for base in node.bases)
            result.update(declaration_atoms(node, qualified, is_enum))
        elif isinstance(node, FUNCTION):
            fields = (node.args, node.returns, *node.decorator_list, *node.type_params)
            result[(owner, node.name, "function")] = tuple(ast.dump(n) if n is not None else "" for n in fields)
        elif isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            kind = "enum" if enum else "sequence" if isinstance(node.value, (ast.List, ast.Tuple, ast.Set)) else "assignment"
            result[(owner, node.targets[0].id, kind)] = ast.dump(node)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            result[(owner, node.target.id, "assignment")] = ast.dump(node)
        else:
            # Imports, namespace mutations, and conditional declarations lack
            # a single safe receiver. Preserve their complete import closure.
            result[(owner, str(other), "other")] = ast.dump(node)
            other += 1
    return result


class Recovery:
    def __init__(self, directory, impact, receivers):
        file = directory / "import_closure.index"
        self.imports = PrecisionIndex(file) if file.is_file() else None
        self.impact = impact
        self.receivers = receivers

    @lru_cache(maxsize=64)
    def clean_atoms(self, path):
        source = self.impact.base(path)[0]
        return declaration_atoms(syntax(path, source)[0])

    def observer_mask(self, path, after):
        previous = self.clean_atoms(path)
        current = declaration_atoms(after)
        edits = [key for key in previous.keys() | current.keys() if previous.get(key) != current.get(key)]
        broad = self.imports.mask(path) or self.impact.full
        if not edits:
            return broad, False
        if any(kind in ("class", "other") for _, _, kind in edits):
            return broad, False
        if len(edits) == 1 and edits[0][2] == "function":
            owner, member, _ = edit = edits[0]
            if edit not in previous:
                inert = self.inert_method(path, after, owner, member)
                if inert is not None:
                    return inert, True
            if edit not in current:
                readers = self.removed_method(path, owner, member)
                if readers is not None:
                    return readers, True
        details = self.impact.details
        module = path.removesuffix(".py").replace("/", ".")
        observed = 0
        direct = True
        for owner, member, kind in edits:
            if not owner:
                if kind != "function":
                    return broad, False
                observed |= self.imports.mask(f"call:{path}:{member}") or self.impact.visitors.get(path, 0)
                direct = False
                continue
            classes = (self.receivers.bits(f"d:{path}:{owner}") |
                       details.references.get(f"{path}:{owner}", 0) |
                       details.types.get(f"{path}:{owner}", 0))
            reads = (self.receivers.bits(f"r:{module}:{owner}:{member}") |
                     details.access.seen.get(f"field:{path}:{owner}:{member}", 0))
            if kind == "function":
                if (owner, member, kind) not in current:
                    reach = classes | self.imports.mask(f"call:{path}:{owner}.{member}")
                else:
                    reach = classes & self.imports.mask("call:" + member)
            elif kind == "enum":
                reach = classes
            elif kind == "sequence":
                # Metaclasses can consume an entire field to construct new
                # lookup tables, before any numbered test reads that table.
                reach = self.receivers.bits(f"r:{module}:{owner}:*")
            else:
                reach = 0
            direct &= bool(reads | reach) and kind in ("function", "assignment") and not member.startswith("__")
            observed |= reads | reach or classes or broad
        return observed or broad, direct and bool(observed)

    def removed_method(self, path, owner, name):
        """Combine qualified call, namespace and member-read observations."""
        if not owner or not self.imports.mask("!attr_references"):
            return None
        module = path.removesuffix(".py").replace("/", ".")
        tag = f"{module}:{owner}"
        if not self.receivers.bits("plain:" + tag):
            return None
        before = syntax(path, self.impact.base(path)[0])[0]
        cls = dict(classes(before.body)).get(owner)
        if cls is None or not any(getattr(node, "name", None) == name and plain_method(node)
                                  for node in cls.body):
            return None
        details = self.impact.details
        return (self.imports.mask(f"call:{path}:{owner}.{name}") |
                self.imports.mask("attr:" + name) |
                self.receivers.bits(f"r:{tag}:{name}") | self.receivers.bits(f"r:{tag}:*") |
                details.access.seen.get(f"field:{path}:{owner}:{name}", 0) |
                details.flow.opaque | getattr(details.flow, "reflect", 0) |
                self.impact.files.get(path, {}).get(0, 0))

    def inert_method(self, path, tree, owner, name):
        """Track namespace readers for a plain, previously unreferenced member."""
        if not owner:
            return None
        tag = path.removesuffix(".py").replace("/", ".") + ":" + owner
        if not self.receivers.bits("plain:" + tag):
            return None
        details = self.impact.details
        known = details.access.names
        if all(known[i // 8] & (1 << (i % 8)) for i in name_bits(name.encode(), len(known))):
            return None
        cls = dict(classes(tree.body)).get(owner)
        if cls is None or not any(getattr(node, "name", None) == name and plain_method(node)
                                  for node in cls.body):
            return None
        return (self.receivers.bits(f"r:{tag}:*") | details.flow.opaque |
                getattr(details.flow, "reflect", 0) | self.impact.files.get(path, {}).get(0, 0))

    @lru_cache(maxsize=8)
    def declarations(self, text):
        if not text or self.imports is None:
            return None
        try:
            edits = split_diff(text)
            declarations = []
            remaining = []
            fixture = any(path.startswith("tests/fixtures/") for path in edits)
            for path, hunks in edits.items():
                if path.startswith("tests/fixtures/"):
                    remaining.append(self.impact.files.get(path, {}).get(0, self.impact.full))
                    continue
                if not path.startswith("sqlglot/") or not path.endswith(".py"):
                    return None
                source, (outer, scopes), _ = self.impact.base(path)
                modified = patched(source, hunks)
                after, (updated, new_scopes), _ = syntax(path, modified)
                if updated != outer or scopes.keys() != new_scopes.keys():
                    declarations.append(self.observer_mask(path, after))
                else:
                    mask = 0
                    calls = self.impact.files.get(path, {})
                    for site, (start, _, digest, _) in scopes.items():
                        if digest != new_scopes[site][2]:
                            mask |= calls.get(start, 0) or self.impact.visitors.get(path, 0)
                    if mask:
                        if fixture:
                            mask |= self.impact.visitors.get(path, 0)
                        remaining.append(mask)
            if not declarations and not (fixture and len(edits) > 1):
                return None
            mask = 0
            for observed, _ in declarations:
                mask |= observed
            for observed in remaining:
                mask |= observed
            direct = len(edits) == 1 and len(declarations) == 1 and declarations[0][1]
            return mask, direct
        except (OSError, SyntaxError, UnicodeError, UnknownChange, ValueError, RecursionError):
            return None
