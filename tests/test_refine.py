import ast
import unittest

from refine import Details, changed_fields, regions


def profile():
    return {
        "bits": ["0", "1", "2", "3", "4", "7"],
        "lines": {"p": {"1": 0}},
        "positions": 0,
        "dialects": {},
        "types": {},
        "references": {},
    }


class BaseObservations(unittest.TestCase):
    def test_short_circuit_uses_whole_old_condition(self):
        old = ast.parse("if (x\n    and y):\n    return_value()")
        new = ast.parse("if (x\n    or y):\n    return_value()")
        self.assertEqual(regions(old, new), [(1, 2)])

    def test_absent_line_uses_callers(self):
        d = Details(profile(), ["a", "b"], ".")
        self.assertEqual(d.statements("path", ast.parse("x=2"), ast.parse("x=3"), 3), 3)

    def test_equivalent_source_keeps_text_observers_and_encoding_guard(self):
        d = Details(profile(), ["a", "b", "c"], ".")
        self.assertEqual(d.equivalent("p", "x=1", "x=1 # note", 2, 7), 2)
        self.assertEqual(d.equivalent("p", "x=1", "# coding: latin-1\nx=1", 2, 7), 7)

    def test_unresolved_and_unobserved_operand_contexts_are_retained(self):
        d = Details(profile(), ["a", "b", "c"], ".")
        d.operand_types["p:1:0:0"] = {"builtin:list": 3, "unresolved": 2}
        self.assertEqual(
            d.comparison("p", ast.parse("x is None"), ast.parse("x == None"), 7), 6
        )
        self.assertEqual(
            d.comparison("p", ast.parse("x is None"), ast.parse("x != None"), 7), 7
        )
        self.assertEqual(
            d.comparison("p", ast.parse("x is None"), ast.parse("y == None"), 7), 7
        )

    def test_enum_identity_requires_both_operands_in_same_verified_enum(self):
        d = Details(profile(), ["a", "b", "c"], ".")
        d.operand_types = {
            "p:1:0:0": {"enum:X": 3, "enum:Y": 2},
            "p:1:0:1": {"enum:X": 7},
        }
        self.assertEqual(
            d.comparison("p", ast.parse("x is X.A"), ast.parse("x == X.A"), 7), 6
        )

    def test_structural_field_check_is_limited_to_existing_literal_values(self):
        old = "class A:\n    TABLE = {'item': get(key=True)}\n"
        self.assertEqual(
            changed_fields(old, old.replace("True", "False")), [("A", "TABLE")]
        )
        for changed in (
            old.replace("'item'", "'other'"),
            old.replace("get(", "assign("),
            old + "raise ImportError()\n",
        ):
            self.assertEqual(changed_fields(old, changed), [])


class ImportCheck(unittest.TestCase):
    def test_valid_syntax_with_failing_initialization_is_rejected(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as directory:
            package = Path(directory) / "sqlglot"
            (package / "dialects").mkdir(parents=True)
            (package / "__init__.py").write_text("")
            (package / "dialects/__init__.py").write_text(
                "class Dialect:\n    classes = {}\n"
            )
            module = "sqlglot/dialects/example.py"
            (Path(directory) / module).write_text("VALUE = 2\n")
            d = Details(profile(), ["a"], directory)
            self.assertTrue(d.import_valid(module, "VALUE = 3\n"))
            self.assertFalse(d.import_valid(module, "VALUE = 1 / 0\n"))
