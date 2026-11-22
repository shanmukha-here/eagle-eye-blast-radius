"""Use clean stress observations to limit the parser test selection."""
import ast
from copy import deepcopy

from impact import patched, split_diff, syntax, UnknownChange
from module_topology import hard_topology, opaque_format
from narrow import imports
from risk_features import edit_lines


def observes_patch(impact, index, parser_bit, text):
    """Retain the stress test for changes to its runtime or startup inputs."""
    if (not index or not parser_bit or not index.mask("module_coverage")
            or not index.mask("count:0") or hard_topology(text) or opaque_format(text)):
        return True
    try:
        edits = split_diff(text)
        if len(edits) > 4:
            return True
        for path, hunks in edits.items():
            fixture_readers = impact.files.get(path, {}).get(0)
            if path.startswith("tests/fixtures/") and path.endswith(".sql") and fixture_readers is not None:
                if fixture_readers & parser_bit:
                    return True
                continue
            if (not path.startswith("sqlglot/") or not path.endswith(".py")
                    or path.endswith("/__init__.py")):
                return True

            cases = index.mask("0:" + path)
            if cases & ~1 or (fixture_readers or 0) & parser_bit:
                return True

            source, (outer, scopes), _ = impact.base(path)
            modified = patched(source, hunks)
            before = syntax(path, source)[0]
            after, (revised, new_scopes), _ = syntax(path, modified)
            if imports(before) != imports(after):
                return True
            declaration = outer != revised or scopes.keys() != new_scopes.keys()
            if declaration:
                if cases or not impact.details.import_valid(path, modified):
                    return True
            if cases and any(index.mask(f"0:{path}:{line}") for line in edit_lines(hunks)[0]):
                return True
        return False
    except (UnknownChange, OSError, SyntaxError, UnicodeError, ValueError, RecursionError):
        return True


def unexecuted_return(impact, text, parser_bit):
    path = "sqlglot/parser.py"
    if not text.startswith("diff --git a/" + path + " "):
        return False
    try:
        edits = split_diff(text)
        if edits.keys() != {path}:
            return False
        source, (skeleton, scopes), old_code = impact.base(path)
        modified = patched(source, edits[path])
        if source.count("\n") != modified.count("\n"):
            return False
        _, (outer, revised), new_code = syntax(path, modified)
        if skeleton != outer or scopes.keys() != revised.keys():
            return False
        changed = [k for k in scopes if scopes[k][2] != revised[k][2]]
        if len(changed) != 1:
            return False
        key = changed[0]
        old, new = scopes[key], revised[key]
        before = old[3]
        after = deepcopy(new[3])
        if not isinstance(before, ast.Module) or not isinstance(after, ast.Module):
            return False
        left = [n for n in ast.walk(before) if isinstance(n, ast.Return)]
        right = [n for n in ast.walk(after) if isinstance(n, ast.Return)]
        modified_returns = [(a, b) for a, b in zip(left, right) if ast.dump(a) != ast.dump(b)]
        if len(left) != len(right) or len(modified_returns) != 1:
            return False
        a, b = modified_returns[0]
        if (a.lineno != a.end_lineno or a.lineno != b.lineno or b.lineno != b.end_lineno
                or not isinstance(a.value, ast.Constant) or not isinstance(b.value, ast.Constant)):
            return False
        b.value = a.value
        if ast.dump(before) != ast.dump(after):
            return False
        lines = impact.details.lines.get(path, {})
        rows = impact.files.get(path, {})
        if not lines.get(a.lineno, 0) or (rows.get(0, 0) | lines[a.lineno]) & parser_bit:
            return False
        if not any(lines.get(line, 0) & parser_bit for line in range(old[0], old[1]+1)):
            return False
        frontier = impact.details.flow.selection(path, old_code, new_code, key, old, new, False, lines)
        return frontier is not None and not frontier & parser_bit
    except (UnknownChange, OSError, SyntaxError, UnicodeError, ValueError, RecursionError, KeyError):
        return False
