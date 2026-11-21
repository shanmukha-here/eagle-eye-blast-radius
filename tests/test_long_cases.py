"""Sample-format checks for the long-test fixture projection."""

import difflib
from pathlib import Path
import tempfile
import unittest

from long_cases import EXECUTE_H, OPTIMIZED_H, FIXTURES, LongCases, fixture_pairs
from precision_index import build


class FixtureCases(unittest.TestCase):
    def test_clean_fixture_parser_retains_meta_and_both_statements(self):
        parsed = fixture_pairs("-- ignore\n# execute: yes\nselect 1;\n\nSELECT 1;\n")
        self.assertEqual(parsed, [({"execute": "yes"}, "select 1", "SELECT 1")])

    def test_replay_targets_only_test_that_checks_the_changed_output(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            path = next(name for name, is_tpch in FIXTURES.items() if is_tpch)
            fixture = home / path
            fixture.parent.mkdir(parents=True)
            before = "select 1;\nSELECT 1;\n"
            after = before.replace("SELECT 1", "SELECT 2")
            fixture.write_text(before)
            patch = f"diff --git a/{path} b/{path}\n" + "".join(difflib.unified_diff(
                before.splitlines(True), after.splitlines(True), fromfile="a/" + path, tofile="b/" + path,
            ))
            build(home / "long_cases.index", {f"count:{EXECUTE_H}": 1}, 1)
            (home / "long_cases.fingerprints").touch()
            app = type("Impact", (), {"root":home})()
            cases = LongCases(home, app, None, [])
            touches = cases.fixture_changes(patch)
            self.assertEqual(touches[EXECUTE_H], 0)
            self.assertEqual(touches[OPTIMIZED_H], 2)
