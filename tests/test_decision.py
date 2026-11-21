import difflib
import tempfile
import unittest
from pathlib import Path

from decision import change_coverage, overlay
from impact import split_diff


class DecisionChecks(unittest.TestCase):
    def test_insertion_neighbors_without_context(self):
        patch = "diff --git a/sqlglot/x.py b/sqlglot/x.py\n@@ -4,0 +5 @@\n+limit = 10000"
        self.assertEqual(change_coverage(patch, {"sqlglot/x.py": {3: 1, 4: 2, 5: 4}}), (6, True))

    def test_overlay_isolates_changed_files_and_shares_clean_ones(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory, "base")
            tree = Path(directory, "overlay")
            tree.mkdir()
            (base / "sqlglot").mkdir(parents=True)
            diff = ""
            for name in ("a", "b", "c"):
                path = f"sqlglot/{name}.py"
                (base / path).write_text("value = 1\n")
                if name != "c":
                    diff += f"diff --git a/{path} b/{path}\n"
                    diff += "".join(difflib.unified_diff(["value = 1\n"], ["value = 2\n"],
                                                        fromfile="a/" + path, tofile="b/" + path))
            overlay(base, tree, split_diff(diff))
            for name in ("a", "b"):
                self.assertEqual((tree / f"sqlglot/{name}.py").read_text(), "value = 2\n")
                self.assertEqual((base / f"sqlglot/{name}.py").read_text(), "value = 1\n")
            self.assertTrue((tree / "sqlglot/c.py").is_symlink())
