"""Apply fine observation masks for verified bodies and dispatch values."""

from impact import UnknownChange, patched, split_diff
from precision_body import evidence, footprint
from precision_index import PrecisionIndex
from registry_keys import one_entry


class Precision:
    def __init__(self, directory, impact, scheduler, query_test):
        self.impact = impact
        self.scheduler = scheduler
        path = directory / "precision.index"
        self.index = PrecisionIndex(path) if path.is_file() else None
        if self.index is not None and self.index.mask("!ready") != impact.full:
            self.index = None
        self.external = impact.details.flow.opaque | impact.details.positions
        self.query_bit = scheduler.bits.get(query_test, 0)
        self.stats = {}

    def protected(self, paths):
        mask = self.external
        for path in paths:
            mask |= self.impact.files.get(path, {}).get(0, 0)
        return mask

    def registry(self, text):
        if self.index is None:
            return None
        try:
            edits = split_diff(text)
            if len(edits) != 1:
                return None
            path, hunks = next(iter(edits.items()))
            if not path.startswith("sqlglot/") or not path.endswith(".py") or path.endswith("/__init__.py"):
                return None
            source, _, _ = self.impact.base(path)
            modified = patched(source, hunks)
            entry = one_entry(self.impact.root, path, source, modified)
            if entry is None:
                return None
            table, key = entry
            name = f"{path}:{table}"
            if not self.index.mask("T:" + name):
                return None
            mask = (self.index.mask("W:" + name) | self.index.mask(f"K:{name}:{key}")
                    | self.protected(edits))
            if mask == self.impact.full or not self.impact.details.import_valid(path, modified):
                return None
            self.stats["dispatch_key"] = True
            return mask
        except (OSError, UnknownChange, SyntaxError, UnicodeError, ValueError, RecursionError):
            return None

    def body(self, text, workers):
        if self.index is None or not workers:
            return workers, None
        changes = footprint(self.impact, text)
        if changes is None:
            return workers, None
        _, changed, anchors, control, branch, outside, queries = evidence(self.index, changes)
        protected = self.protected(changes)
        observers = changed | anchors | control | branch | protected
        original = {n for worker in workers for n in worker}
        retained = {n for n in original
                    if not self.scheduler.bits[n] or self.scheduler.bits[n] & observers}
        if retained != original:
            self.stats["line_cleared"] = len(original - retained)
            workers = self.scheduler.repack(retained, workers)
        return workers, None if outside or protected & self.query_bit else queries
