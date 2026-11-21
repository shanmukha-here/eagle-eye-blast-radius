"""Resolve fallback selections from clean module dependencies and patched syntax."""
import ast
import json
from functools import lru_cache
from pathlib import Path

from impact import patched, split_diff, structure, UnknownChange


def imports(tree):
    edges = []
    def visit(node, context):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            edges.append((context, ast.dump(node)))
            return
        if isinstance(node, (ast.If, ast.IfExp, ast.While)):
            context += (ast.dump(node.test),)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            context += (node.name,)
        for field, value in ast.iter_fields(node):
            if isinstance(value, list):
                for child in value:
                    if isinstance(child, ast.AST):
                        visit(child, context + (field,))
            elif isinstance(value, ast.AST):
                visit(value, context + (field,))
    visit(tree, ())
    return edges


def literal_defaults(old, new):
    """A replacement of existing literal defaults preserves definition identity."""
    left, right = ast.parse(old), ast.parse(new)
    changed = []
    functions = (ast.FunctionDef, ast.AsyncFunctionDef)
    for a, b in zip(ast.walk(left), ast.walk(right)):
        if type(a) != type(b) or not isinstance(a, functions):
            continue
        defaults = a.args.defaults + a.args.kw_defaults
        revised = b.args.defaults + b.args.kw_defaults
        if len(defaults) != len(revised) or a.decorator_list or b.decorator_list:
            continue
        if any(isinstance(x, ast.Constant) and isinstance(y, ast.Constant) and
               type(x.value) == type(y.value) and x.value != y.value for x,y in zip(defaults, revised)):
            if all((x is None and y is None) or
                   (isinstance(x, ast.Constant) and isinstance(y, ast.Constant) and type(x.value)==type(y.value))
                   for x,y in zip(defaults, revised)):
                changed.append((a.lineno, a.end_lineno))
                b.args.defaults, b.args.kw_defaults = a.args.defaults, a.args.kw_defaults
    return changed if changed and ast.dump(left) == ast.dump(right) else []



def fixture_inputs(text):
    # Mirrors load_sql_fixture_pairs on the pin. Only expected-output edits are eligible.
    pieces = '\n'.join(line for line in text.splitlines() if line and not line.startswith('--')).split(';')
    return len(pieces), pieces[::2]


class Narrow:
    def __init__(self, impact, directory):
        self.impact = impact
        self.details = impact.details
        by_id = {name: 1 << i for i, name in enumerate(self.details.ids)}
        reads = json.loads((Path(directory) / 'fixture_reads.json').read_bytes())
        self.fixture_unused = {
            path: sum(by_id.get(name, 0) for name, used in rows.items() if not used)
            for path, rows in reads.items()
        }

    @lru_cache(maxsize=128)
    def original(self, path):
        source = (self.impact.root / path).read_text()
        tree = ast.parse(source)
        return source, imports(tree), structure(tree)

    def module(self, path, hunks):
        full = self.impact.full
        if path not in self.details.lines or not path.endswith('.py'):
            return full
        source, dependencies, (skeleton, before) = self.original(path)
        modified = patched(source, hunks)
        compile(modified, path, 'exec', dont_inherit=True)
        tree = ast.parse(modified)
        if imports(tree) != dependencies:
            return full
        after_skeleton, after = structure(tree)
        readers = self.impact.files.get(path, {}).get(0, 0)
        group = self.details.imports.get(path, full)
        if path.startswith('tests/'):
            if Path(path).name == 'conftest.py':
                return full
            return group | self.details.test_files.get(path, 0) | readers
        if path.startswith('sqlglot/') and skeleton == after_skeleton and before.keys() == after.keys():
            changed = [k for k in before if before[k][2] != after[k][2]]
            if changed and all(not k[-2].startswith('__') for k in changed):
                selected = readers | self.details.flow.opaque
                for k in changed:
                    start, end, _, _ = before[k]
                    for ln, mask in self.details.lines[path].items():
                        if start <= ln <= end:
                            selected |= mask
                # Import-time execution can propagate the change to dependent modules.
                if selected != full and self.details.import_valid(path, modified):
                    return selected
        if path.startswith('sqlglot/') and skeleton != after_skeleton:
            ranges = literal_defaults(source, modified)
            if ranges:
                selected = readers | self.details.flow.opaque | self.details.positions
                for line, mask in self.details.lines[path].items():
                    if any(start <= line <= end for start, end in ranges):
                        selected |= mask
                if selected != full and self.details.import_valid(path, modified):
                    return selected
        projected = group | self.impact.visitors.get(path, full) | readers | self.details.flow.opaque
        if projected == full or not path.startswith('sqlglot/') or not self.details.import_valid(path, modified):
            return full
        return projected

    def select(self, text, selected):
        full = self.impact.full
        try:
            if selected != full and not any(path in text for path in self.fixture_unused):
                return selected
            edits = split_diff(text)
            if len(edits) == 1:
                path, hunks = next(iter(edits.items()))
                if path in self.fixture_unused:
                    source = (self.impact.root / path).read_text()
                    if fixture_inputs(source) == fixture_inputs(patched(source, hunks)):
                        return selected & ~self.fixture_unused[path]
            if selected != full:
                return selected
            result = 0
            for path, hunks in edits.items():
                mask = full if len(edits) == 1 else self.impact.file_mask(path, hunks)
                if mask == full:
                    mask = self.module(path, hunks)
                result |= mask
            return result
        except (UnknownChange, SyntaxError, OSError, ValueError, UnicodeError, RecursionError, KeyError):
            return selected
