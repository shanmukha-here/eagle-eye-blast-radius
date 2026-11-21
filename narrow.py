"""Resolve fallback selections from clean module dependencies and patched syntax."""
import ast
import json
from functools import lru_cache
from pathlib import Path

from impact import patched, split_diff, structure, UnknownChange


def imports(tree):
    return [ast.dump(n) for n in ast.walk(tree) if isinstance(n, (ast.ImportFrom, ast.Import))]


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
        if not path.startswith('sqlglot/') or not self.details.import_valid(path, modified):
            return full
        return group | self.impact.visitors.get(path, full) | readers | self.details.flow.opaque

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
