"""Estimate test impact from verified base text and per-test coverage."""

import ast
import re
from functools import lru_cache
from pathlib import Path

from refine import bindings

HEADER = re.compile(r"diff --git a/([A-Za-z0-9_./-]+) b/([A-Za-z0-9_./-]+)")
HUNK = re.compile(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@.*")
FUNCTION = (ast.FunctionDef, ast.AsyncFunctionDef)


class UnknownChange(Exception):
    pass


def split_diff(text):
    """Accept same-path textual unified diffs. Anything else uses all tests."""
    lines = text.splitlines()
    if not lines or len(lines) > 25000:
        raise UnknownChange
    files = {}
    i = 0
    while i < len(lines):
        header = HEADER.fullmatch(lines[i])
        if not header or header[1] != header[2]:
            raise UnknownChange
        path = header[1]
        if path in files or any(part in ("", ".", "..") for part in path.split("/")):
            raise UnknownChange
        i += 1
        if i < len(lines) and lines[i].startswith("index "):
            i += 1
        if lines[i : i + 2] != ["--- a/" + path, "+++ b/" + path]:
            raise UnknownChange
        i += 2
        hunks = []
        while i < len(lines) and not lines[i].startswith("diff --git "):
            match = HUNK.fullmatch(lines[i])
            if not match:
                raise UnknownChange
            a, an, b, bn = match.groups()
            old_count = int(an or 1)
            new_count = int(bn or 1)
            start = int(a) - int(old_count > 0)
            new_start = int(b) - int(new_count > 0)
            old, new = [], []
            i += 1
            while len(old) < old_count or len(new) < new_count:
                if i == len(lines) or not lines[i] or lines[i][0] not in " +-":
                    raise UnknownChange
                sign, line = lines[i][0], lines[i][1:]
                if sign != "+":
                    old.append(line)
                if sign != "-":
                    new.append(line)
                i += 1
            if (
                len(old) != old_count
                or len(new) != new_count
                or start < 0
                or new_start < 0
            ):
                raise UnknownChange
            hunks.append((start, new_start, old, new))
        if not hunks:
            raise UnknownChange
        files[path] = hunks
        if len(files) > 32:
            raise UnknownChange
    return files


def patched(source, hunks):
    original = source.splitlines()
    result = []
    offset = 0
    for start, new_start, old, new in hunks:
        if (
            start < offset
            or start > len(original)
            or original[start : start + len(old)] != old
        ):
            raise UnknownChange
        result.extend(original[offset:start])
        if len(result) != new_start:
            raise UnknownChange
        result.extend(new)
        offset = start + len(old)
    result.extend(original[offset:])
    return "\n".join(result) + "\n"


def structure(tree):
    """Separate function bodies from the definitions executed at import time."""
    functions = {}
    occurrences = {}

    def visit(node, path):
        if isinstance(node, (*FUNCTION, ast.Lambda)):
            is_lambda = isinstance(node, ast.Lambda)
            name = "<lambda>" if is_lambda else node.name
            group = (*path, name)
            ordinal = occurrences.get(group, 0)
            occurrences[group] = ordinal + 1
            # Overloads, setters, and dispatch-table lambdas can share a name.
            # The unchanged outer syntax guarantees the same traversal order.
            key = (*group, ordinal)
            if is_lambda:
                first = node.lineno
                body = node.body
                node.body = ast.Constant(value=None)
            else:
                first = min([node.lineno] + [d.lineno for d in node.decorator_list])
                body = ast.Module(body=node.body, type_ignores=[])
                node.body = [ast.Pass()]
            functions[key] = (first, node.end_lineno, ast.dump(body), body)
            return
        child_path = (*path, node.name) if isinstance(node, ast.ClassDef) else path
        for child in ast.iter_child_nodes(node):
            visit(child, child_path)

    visit(tree, ())
    return ast.dump(tree), functions


class Impact:
    def __init__(self, root, profile, details=None):
        self.details = details
        self.root = Path(root)
        self.files = {
            path: {int(k): int(v, 16) for k, v in rows.items()}
            for path, rows in profile["files"].items()
        }
        self.visitors = {
            path: int(mask, 16) for path, mask in profile["visitors"].items()
        }
        self.full = (1 << len(profile["ids"])) - 1

    @lru_cache(maxsize=96)
    def base(self, path):
        source = (self.root / path).read_text("utf-8")
        return (
            source,
            structure(ast.parse(source)),
            compile(source, path, "exec", dont_inherit=True),
        )

    def file_mask(self, path, hunks, single=False):
        rows = self.files.get(path, {})
        if path.startswith("tests/fixtures/"):
            return rows.get(0, self.full)
        if (
            self.details
            and path.startswith("tests/")
            and path.endswith(".py")
            and path in self.details.lines
        ):
            source, original, _ = self.base(path)
            modified = patched(source, hunks)
            compile(modified, path, "exec", dont_inherit=True)
            return self.details.test_body(
                path, original, structure(ast.parse(modified))
            )
        if not path.startswith("sqlglot/") or not path.endswith(".py") or not rows:
            return self.full
        source, (skeleton, scopes), original_code = self.base(path)
        modified = patched(source, hunks)
        # Compilation also detects scoping errors that ast.parse alone accepts.
        updated_code = compile(modified, path, "exec", dont_inherit=True)
        updated, new_scopes = structure(ast.parse(modified))
        if skeleton != updated or scopes.keys() != new_scopes.keys():
            if self.details and single:
                return self.details.fields(
                    path, source, modified, rows.get(0, 0), self.visitors.get(path, 0)
                )
            return self.full
        affected = [k for k in scopes if scopes[k][2] != new_scopes[k][2]]
        readers = rows.get(0, 0)
        if not affected:
            # Non-semantic edits can still move line information or affect source readers.
            fallback = readers | self.visitors.get(path, self.full)
            return (
                self.details.equivalent(path, source, modified, readers, fallback)
                if self.details
                else fallback
            )
        selected = readers
        refine = self.details and bindings(original_code) == bindings(updated_code)
        for key in affected:
            start, end, _, old_body = scopes[key]
            # Include nested functions and lambdas created by the enclosing function.
            if isinstance(old_body, ast.Module) and ast.get_docstring(
                old_body, clean=False
            ) != ast.get_docstring(new_scopes[key][3], clean=False):
                return self.full
            if start not in rows:
                return self.full
            wide = 0
            for first, observed in rows.items():
                if start <= first <= end:
                    wide |= observed
            if refine:
                new_body = new_scopes[key][3]
                wide = self.details.statements(path, old_body, new_body, wide)
                if single and len(affected) == 1:
                    wide = self.details.comparison(path, old_body, new_body, wide)
            selected |= wide
        return selected

    def select(self, patch):
        try:
            selection = 0
            edits = split_diff(patch)
            for path, hunks in edits.items():
                selection |= self.file_mask(path, hunks, single=len(edits) == 1)
                if selection == self.full:
                    break
            return selection
        except (
            UnknownChange,
            OSError,
            UnicodeError,
            SyntaxError,
            ValueError,
            KeyError,
            RecursionError,
            OverflowError,
        ):
            return self.full
