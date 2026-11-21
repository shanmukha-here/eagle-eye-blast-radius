"""Reuse the clean forced-import verdict for untouched execution paths."""

from impact import syntax
from precision_index import PrecisionIndex


class ColdImport:
    def __init__(self, file, impact):
        self.index = PrecisionIndex(file)
        self.impact = impact
        self.shortcuts = 0

    def __call__(self, path, modified):
        read = self.index.mask
        if not read(path):
            return False
        try:
            _, (outer, before), _ = self.impact.base(path)
            _, (revised, after), _ = syntax(path, modified)
            if revised != outer or before.keys() != after.keys():
                return False
            changed = [key for key, value in before.items() if value[2] != after[key][2]]
            if not changed:
                return False
            # Function execution can affect import even when top-level syntax
            # is unchanged. Retain the real check for every recorded call.
            if any(before[key][:2] != after[key][:2] or read(f"{path}:{key[-2]}") for key in changed):
                return False
        except (OSError, SyntaxError, ValueError, UnicodeError, RecursionError):
            return False
        self.shortcuts += 1
        return True
