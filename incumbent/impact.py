"""Estimate test impact from verified base text and per-test coverage."""

import ast
from functools import lru_cache
from pathlib import Path

from dependencies import appended_failure, simple_collection
from .refine import bindings

from impact import UnknownChange, new_source, patched, split_diff, syntax


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
        _, scopes, code = syntax(path, source)
        return source, scopes, code

    def file_mask(self, path, hunks, single=False, fixture_mix=False):
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
            grouped = (
                self.details.imports.get(path, self.full)
                | self.details.test_files.get(path, self.full)
                | rows.get(0, 0)
            )
            numbered_module = (
                single and path in self.details.test_files
                and Path(path).name.startswith("test_")
            )
            try:
                _, after, _ = syntax(path, modified)
            except SyntaxError:
                return grouped if numbered_module and simple_collection(source) else self.full
            if numbered_module and appended_failure(source, modified):
                return grouped
            return self.details.test_body(
                path, original, after
            )
        if not path.startswith("sqlglot/") or not path.endswith(".py") or not rows:
            return self.full
        source, (skeleton, scopes), original_code = self.base(path)
        modified = patched(source, hunks)
        # Compilation also detects scoping errors that ast.parse alone accepts.
        new_tree, (updated, new_scopes), updated_code = syntax(path, modified)
        flow = getattr(self.details, "flow", None)
        if skeleton != updated or scopes.keys() != new_scopes.keys():
            if flow and single and flow.equality(path, syntax(path, source)[0], new_tree):
                return rows.get(0, 0) | flow.opaque
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
        flow_mask = None
        if flow and (single or fixture_mix) and len(affected) == 1:
            key = affected[0]
            if single and flow.equality(path, scopes[key][3], new_scopes[key][3]):
                flow_mask = flow.opaque
            else:
                uncalled = not any(scopes[key][0] <= line <= scopes[key][1] for line in rows)
                flow_mask = flow.selection(path, original_code, updated_code, key, scopes[key], new_scopes[key], uncalled, self.details.lines.get(path))
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
                return self.full if flow_mask is None else (readers | flow_mask)
            wide = 0
            for first, observed in rows.items():
                if start <= first <= end:
                    wide |= observed
            if refine:
                new_body = new_scopes[key][3]
                wide = self.details.statements(path, old_body, new_body, wide)
                if single and len(affected) == 1:
                    wide = self.details.comparison(path, old_body, new_body, wide)
            selected |= wide if flow_mask is None else wide & flow_mask
        replay = getattr(self.details, "replay", None)
        if (single and len(affected) == 1 and path.startswith("sqlglot/")
                and replay and not readers & replay.test):
            key = affected[0]
            selected = replay.select(path, modified, key, scopes[key], new_scopes[key], selected,
                                     getattr(self, "scheduler", None))
        return selected

    def select(self, patch):
        try:
            selection = 0
            if self.details and self.details.access and "--- /dev/null" in patch:
                addition = new_source(patch)
                if addition is not None:
                    return self.details.access.addition(self.root, *addition, self.full)
            edits = split_diff(patch)
            # Fixture files are also covered by their original readers. For one
            # production edit plus fixtures, an unselected execution must follow
            # the old path up to its first changed instruction or fixture read.
            fixture_mix = (len(edits) > 1
                           and sum(p.startswith("sqlglot/") and p.endswith(".py") for p in edits) == 1
                           and all(p.startswith(("sqlglot/", "tests/fixtures/")) for p in edits)
                           and sum(p.startswith("sqlglot/") for p in edits) == 1)
            for path, hunks in edits.items():
                selection |= self.file_mask(path, hunks, single=len(edits) == 1, fixture_mix=fixture_mix)
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
