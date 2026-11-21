import ast
import unittest

from observation_index import default_changes
from value_observations import input_mask, replacements


class ScalarObservationsTest(unittest.TestCase):
    def test_short_circuit_distinguishes_only_changed_outputs(self):
        old = ast.parse("def f(a, b):\n return None if a is None or b is None else True")
        new = ast.parse("def f(a, b):\n return None if a is None and b is None else True")
        [(before, after, names, predicate)] = replacements(old, new)
        self.assertEqual(
            input_mask([((None, True), 1), ((None, None), 2), ((True, False), 4), (None, 8)],
                       names, before, after, predicate),
            (15, 9),
        )

    def test_checks_nested_predicate_without_evaluating_its_calling_branch(self):
        old = ast.parse("def f(a, b):\n x = 0 if a < 0 else b.explode()")
        new = ast.parse("def f(a, b):\n x = 0 if a <= 0 else b.explode()")
        [(before, after, names, predicate)] = replacements(old, new)
        self.assertEqual(input_mask([((0,), 1), ((-10,), 2)], names, before, after, predicate), (3, 1))

    def test_refuses_pure_claim_for_different_call_branch(self):
        old = ast.parse("def f(a, b):\n x = 0 if a < 0 else b.old()")
        new = ast.parse("def f(a, b):\n x = 0 if a <= 0 else b.new()")
        self.assertEqual(replacements(old, new), [])

    def test_default_requires_same_body_and_signature(self):
        old = ast.parse("def f(a, /, b=True, *, c='x'):\n return b")
        new = ast.parse("def f(a, /, b=False, *, c='y'):\n return b")
        self.assertEqual(default_changes(old, new), [(1, "f", "b"), (1, "f", "c")])
        self.assertEqual(default_changes(old, ast.parse("def f(a, /, b=False, *, c='y'):\n return c")), [])



class OverlayBytecodeTest(unittest.TestCase):
    def test_reuses_only_unchanged_module_bytecode(self):
        import py_compile
        from pathlib import Path
        from tempfile import TemporaryDirectory

        from decision import overlay

        with TemporaryDirectory() as location:
            root = Path(location)
            base = root / "base"
            directory = base / "sqlglot"
            directory.mkdir(parents=True)
            changed = directory / "edited.py"
            clean = directory / "clean.py"
            changed.write_text("value = 0\n")
            clean.write_text("value = 1\n")
            stale = Path(py_compile.compile(str(changed))).name
            reusable = Path(py_compile.compile(str(clean))).name
            patched = root / "patched"
            patched.mkdir()
            overlay(base, patched, {"sqlglot/edited.py": [(0, 0, ["value = 0"], ["value = 2"])]})
            self.assertEqual((patched / "sqlglot/edited.py").read_text(), "value = 2\n")
            cache = patched / "sqlglot/__pycache__"
            self.assertFalse((cache / stale).exists())
            self.assertTrue((cache / reusable).is_symlink())


if __name__ == "__main__":
    unittest.main()
