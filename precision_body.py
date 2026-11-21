"""Project isolated function-body changes onto the original executed statements."""
import ast
from difflib import SequenceMatcher

from impact import UnknownChange, patched, split_diff, syntax
from refine import bindings, dump


CONTROL = (ast.If, ast.While, ast.For, ast.AsyncFor, ast.With, ast.AsyncWith)
DEFINITIONS = (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
OPAQUE = (ast.Yield, ast.YieldFrom, ast.Global, ast.Nonlocal, ast.Import, ast.ImportFrom)


def body_regions(before, after):
    """Return original line regions, including control entries around insertions."""
    bounds, anchors, conditions = set(), set(), set()

    def mark(target, node):
        target.add((node.lineno, node.end_lineno))

    def blocks(a, b, entry):
        original, revised = [dump(n) for n in a], [dump(n) for n in b]
        for tag, i, j, k, stop in SequenceMatcher(None, original, revised, autojunk=False).get_opcodes():
            if tag == "equal":
                continue
            if tag == "replace" and j - i == stop - k:
                for x, y in zip(a[i:j], b[k:stop]):
                    statement(x, y)
            elif tag == "insert":
                if entry is not None and i == 0:
                    anchors.add((entry, entry))
                if i:
                    # A preceding conditional can finish on either arm. Its
                    # entry witnesses reach even when its last line did not run.
                    anchors.add((a[i - 1].lineno, a[i - 1].lineno))
                if i < len(a):
                    anchors.add((a[i].lineno, a[i].lineno))
            else:
                for x in a[i:j]:
                    mark(bounds, x)
                if tag == "replace" and i:
                    anchors.add((a[i - 1].lineno, a[i - 1].lineno))

    def statement(a, b):
        if isinstance(a, CONTROL) and type(a) is type(b):
            parts = ("body", "orelse")
            def control(node):
                return tuple(tuple(map(dump, value)) if isinstance(value, list) else dump(value)
                             for key, value in ast.iter_fields(node) if key not in parts)
            old, new = control(a), control(b)
            if old != new:
                last = a.body[0].lineno
                conditions.add((a.lineno, last))
            blocks(a.body, b.body, a.lineno)
            if hasattr(a, "orelse"):
                blocks(a.orelse, b.orelse, a.lineno)
            return
        mark(bounds, a)

    if not isinstance(before, ast.Module) or not isinstance(after, ast.Module):
        return None
    if ast.get_docstring(before, clean=False) != ast.get_docstring(after, clean=False):
        return None
    blocks(before.body, after.body, None)
    return bounds, anchors, conditions


def footprint(impact, text):
    """Bound a diff to compiled, same-binding bodies with no namespace edits."""
    try:
        edits = split_diff(text)
        if not edits or len(edits) > 4:
            return None
        changes = {}
        for path, hunks in edits.items():
            if not path.startswith("sqlglot/") or not path.endswith(".py") or path.endswith("/__init__.py"):
                return None
            source, (outer, scopes), code = impact.base(path)
            other = patched(source, hunks)
            _, (skeleton, new_scopes), changed_code = syntax(path, other)
            if outer != skeleton or scopes.keys() != new_scopes.keys() or bindings(code) != bindings(changed_code):
                return None
            rows = []
            for site, before in scopes.items():
                after = new_scopes[site]
                if before[2] == after[2]:
                    continue
                if site[-2].startswith(("<", "__")) or not isinstance(before[3], ast.Module):
                    return None
                if any(isinstance(node, (*DEFINITIONS, *OPAQUE))
                       for body in (before[3], after[3]) for node in ast.walk(body)):
                    return None
                ranges = body_regions(before[3], after[3])
                if not ranges or not any(ranges):
                    return None
                rows.append((site, before[:2], ranges))
            if rows:
                changes[path] = rows
        return changes or None
    except (OSError, UnknownChange, UnicodeError, SyntaxError, RecursionError, ValueError):
        return None


def range_mask(read, prefix, path, regions):
    result = 0
    for start, end in regions:
        for line in range(start, end + 1):
            result |= read(f"{prefix}:{path}:{line}")
    return result


def evidence(index, changes):
    function = changed = inserted = condition = outside = queries = branches = 0
    for path, records in changes.items():
        for site, (first, _), (body, anchors, control) in records:
            qual = ".".join(site[:-1])
            function |= index.mask(f"F:{path}:{first}:{qual}")
            changed |= range_mask(index.mask, "L", path, body)
            inserted |= range_mask(index.mask, "L", path, anchors)
            condition |= range_mask(index.mask, "L", path, control)
            branches |= range_mask(index.mask, "BL", path, control)
            outside |= range_mask(index.mask, "OL", path, body | anchors | control)
            queries |= range_mask(index.mask, "QL", path, body | anchors | control)
    return function, changed, inserted, condition, branches, outside, queries
