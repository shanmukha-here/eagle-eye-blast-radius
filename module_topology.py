"""Identify diff forms that can invalidate clean import observations."""
import re

DIFF = re.compile(r"^diff --git a/(\S+) b/(\S+)$", re.MULTILINE)


def hard_topology(patch):
    # Clean importers are incomplete when an earlier import warmed the module
    # cache. Deletions and renames need the entire set until cold collection
    # and the transitive dependency graph can certify a smaller one.
    if re.search(r"^(?:deleted file mode |rename (?:from|to) |\+\+\+ /dev/null$)",
                 patch, re.MULTILINE):
        return True
    return any(before != after for before, after in DIFF.findall(patch))


def addition(patch):
    return bool(re.search(r"^(?:new file mode |--- /dev/null$)", patch, re.MULTILINE))
