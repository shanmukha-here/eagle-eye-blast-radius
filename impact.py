"""Estimate test impact from verified base text and per-test coverage."""

import ast
import re
from functools import lru_cache
from pathlib import Path

from dependencies import appended_failure, simple_collection
from refine import bindings
from miniatures import CORE

HEADER = re.compile(r"diff --git a/([A-Za-z0-9_./-]+) b/([A-Za-z0-9_./-]+)")
HUNK = re.compile(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@.*")
FUNCTION = (ast.FunctionDef, ast.AsyncFunctionDef)


class UnknownChange(Exception):
    pass


def new_source(patch):
    """Accept a single ordinary Git file addition from /dev/null."""
    lines = patch.splitlines()
    if not lines or len(lines) > 25000:
        return None
    header = HEADER.fullmatch(lines[0])
    if not header or header[1] != header[2]:
        return None
    path = header[1]
    if not re.fullmatch(r"sqlglot/[a-z][a-z0-9_]*\.py", path):
        return None
    i = 1
    if i < len(lines) and lines[i] in ("new file mode 100644", "new file mode 100755"):
        i += 1
    if i < len(lines) and lines[i].startswith("index "):
        i += 1
    if lines[i:i+2] != ["--- /dev/null", "+++ b/" + path] or i+2 >= len(lines):
        return None
    hunk = HUNK.fullmatch(lines[i+2])
    added = lines[i+3:]
    if (not hunk or (hunk[1], hunk[2]) != ("0", "0") or hunk[3] != "1"
            or int(hunk[4] or 1) != len(added) or not all(line.startswith("+") for line in added)):
        return None
    return path, "\n".join(line[1:] for line in added) + "\n"


@lru_cache(maxsize=16)
def split_diff(text, *, physical=False):
    """Accept same-path textual unified diffs. Anything else uses all tests."""
    if physical:
        if not text.endswith("\n"):
            raise UnknownChange
        lines = text[:-1].split("\n")
    else:
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


def patched(source, hunks, *, physical=False):
    edits = tuple((i, j, tuple(old), tuple(new)) for i, j, old, new in hunks)
    return apply_hunks(source, edits, physical)


@lru_cache(maxsize=16)
def apply_hunks(source, hunks, physical=False):
    if physical:
        if not source.endswith("\n"):
            raise UnknownChange
        original = tuple(source[:-1].split("\n"))
    else:
        original = tuple(source.splitlines())
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


# Repeated independent edits often touch different functions in one module.
# Exact source bytes let unchanged bodies reuse their canonical AST dump.
_BODY_DUMPS = {}


def structure(tree, source=None):
    """Separate function bodies from the definitions executed at import time."""
    functions = {}
    occurrences = {}
    restore = []
    lines = source.replace("\r\n", "\n").replace("\r", "\n").split("\n") if source is not None else None

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
            restore.append((node, node.body))
            if is_lambda:
                first = node.lineno
                body = node.body
                node.body = ast.Constant(value=None)
            else:
                first = min([node.lineno] + [d.lineno for d in node.decorator_list])
                body = ast.Module(body=node.body, type_ignores=[])
                node.body = [ast.Pass()]
            if lines is None or is_lambda:
                digest = ast.dump(body)
            else:
                segment = "\n".join(lines[node.lineno - 1:node.end_lineno])
                digest = _BODY_DUMPS.get(segment)
                if digest is None:
                    digest = ast.dump(body)
                    if len(_BODY_DUMPS) >= 8192:
                        _BODY_DUMPS.clear()
                    _BODY_DUMPS[segment] = digest
            functions[key] = (first, node.end_lineno, digest, body)
            return
        child_path = (*path, node.name) if isinstance(node, ast.ClassDef) else path
        for child in ast.iter_child_nodes(node):
            visit(child, child_path)

    try:
        visit(tree, ())
        return ast.dump(tree), functions
    finally:
        for node, body in restore:
            node.body = body


@lru_cache(maxsize=48)
def syntax_tree(path, source):
    return ast.parse(source, path)


@lru_cache(maxsize=24)
def syntax(path, source):
    """Share the parsed and compiled source across selectors and fallback checks."""
    tree = syntax_tree(path, source)
    code = compile(tree, path, "exec", dont_inherit=True)
    return tree, structure(tree, source), code


def bypasses_executor_result(old, new):
    previous = [n for n in ast.walk(old) if isinstance(n, ast.Return)]
    current = [n for n in ast.walk(new) if isinstance(n, ast.Return)]
    return len(previous) == len(current) and any(
        isinstance(a.value, ast.Call) and isinstance(b.value, ast.Constant) and b.value.value is None
        for a, b in zip(previous, current)
    )


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

    def check_startup(self, mask):
        # An import edit can affect interpreter-wide timing. The scaled parser
        # assertions do not certify that the full stress run finishes in time.
        miniature = getattr(self.details, "miniatures", None)
        return mask | miniature.tests.get("parser", 0) if miniature else mask

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
        startup = getattr(self.details, "startup", None) if single else None
        if skeleton != updated or scopes.keys() != new_scopes.keys():
            if startup:
                registration = startup.registry(path, source, modified, self.details.access)
                if registration is not None and self.details.import_valid(path, modified):
                    mask, default_key_only = registration
                    selected = self.check_startup(mask | rows.get(0, 0) | flow.opaque)
                    if default_key_only and not (mask | rows.get(0, 0) | flow.opaque) & self.details.miniatures.tests.get("parser", 0):
                        # get_or_raise(None) constructs the base class directly.
                        # Require no traced reads of any affected derived field, then verify cold.
                        return self.details.miniatures.check(selected, self.scheduler, "parser", path, modified)
                    return selected
            if flow and single and flow.equality(path, syntax(path, source)[0], new_tree):
                return rows.get(0, 0) | flow.opaque
            if self.details and single:
                return self.details.fields(
                    path, source, modified, rows.get(0, 0), self.visitors.get(path, 0)
                )
            return self.full
        affected = [k for k in scopes if scopes[k][2] != new_scopes[k][2]]
        readers = rows.get(0, 0)
        if len(affected) == 1 and startup:
            key = affected[0]
            mask = startup.lazy(path, key, scopes[key][3], new_scopes[key][3])
            if mask is not None:
                return self.check_startup(mask | readers | flow.opaque)
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
                                     getattr(self, "scheduler", None), bool(refine))
        miniature = getattr(self.details, "miniatures", None)
        if miniature and single and len(affected) == 1:
            key = affected[0]
            selected = miniature.select(selected, getattr(self, "scheduler", None), path, modified,
                                        key, scopes[key], new_scopes[key], readers)
        # Executor code runs in child workers during the suite. An early return
        # that bypasses a result constructor can disrupt their shared lifetime.
        if miniature and path.startswith("sqlglot/executor/"):
            for key in affected:
                if bypasses_executor_result(scopes[key][3], new_scopes[key][3]):
                    selected |= miniature.tests.get("parser", 0)
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
            miniature = getattr(self.details, "miniatures", None)
            if miniature and selection != self.full and edits.keys() & CORE:
                if len(edits) != 1:
                    return selection | miniature.tests.get("parser", 0)
                path, hunks = next(iter(edits.items()))
                source, before, _ = self.base(path)
                modified = patched(source, hunks)
                return miniature.protect_core(selection, self.scheduler, path, modified,
                                              before, syntax(path, modified)[1])
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
