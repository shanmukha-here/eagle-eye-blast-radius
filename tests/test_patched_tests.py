from pathlib import Path
from types import SimpleNamespace
import unittest

from exact_tests import UnitOutcomes
from patched_tests import Outcomes
from warm_tests import install


class PatchedTestEvidence(unittest.TestCase):
    def test_failed_subtest_does_not_stop_other_methods(self):
        visited = []

        class Example(unittest.TestCase):
            def test_bad(self):
                with self.subTest():
                    visited.append("failed")
                    self.fail("first subtest")
                with self.subTest():
                    visited.append("unneeded")

            def test_good(self):
                visited.append("passed")

        result = UnitOutcomes(continue_after_failure=True)
        result.failfast = True
        unittest.defaultTestLoader.loadTestsFromTestCase(Example).run(result)
        self.assertEqual(visited, ["failed", "passed"])
        self.assertEqual(len(result.failures), 1)
        self.assertEqual(len(result.passed), 1)

    def test_caught_timeout_still_cannot_certify_test(self):
        result = UnitOutcomes()
        case = PatchedTestEvidence("test_caught_timeout_still_cannot_certify_test")
        result.startTest(case)
        with self.assertRaises(TimeoutError):
            result.on_timeout(None, None)
        result.addSuccess(case)
        self.assertFalse(result.passed)

    def test_completed_assertions_need_successful_shared_teardown(self):
        a, b, c = "test_same.py::a", "test_same.py::b", "test_other.py::c"
        outcomes = Outcomes([a, b, c])
        for node in (a, b, c):
            for phase in ("setup", "call", "teardown"):
                outcomes.pytest_runtest_logreport(SimpleNamespace(
                    nodeid=node, when=phase, passed=(node, phase) != (b, "teardown")
                ))
        self.assertEqual(outcomes.passed(), [c])
        outcomes.pytest_collectreport(SimpleNamespace(failed=True))
        self.assertEqual(outcomes.passed(), [])

    def test_never_replace_a_function_that_ran_at_import(self):
        from tempfile import TemporaryDirectory

        path = "sqlglot/m.py"
        namespace = {}
        exec(compile("def f():\n    return 1\n", path, "exec"), namespace)
        original = namespace["f"]
        with TemporaryDirectory() as work:
            root = Path(work)
            (root / path).parent.mkdir()
            (root / path).write_text("def f():\n    return 2\n")
            replacements = {path: ("f",)}
            live = {(path, "f"): [original]}
            self.assertFalse(install(root, replacements, {(path, "f")}, live))
            self.assertEqual(original(), 1)
            self.assertTrue(install(root, replacements, set(), live))
            self.assertEqual(original(), 2)
