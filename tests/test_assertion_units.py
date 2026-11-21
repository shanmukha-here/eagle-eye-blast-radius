import ast
import unittest

from assertion_units import admissible, literals, safe_body


class _Example(unittest.TestCase):
    __test__ = False

    def validate_identity(self, sql, **options):
        pass

    def test_independent_calls(self):
        self.validate_identity("SELECT 1")
        for sql in ("SELECT 2", "SELECT 3"):
            with self.subTest(sql=sql):
                self.validate_identity(sql)

    def test_state_crosses_calls(self):
        parsed = self.validate_identity("SELECT 1")
        self.assertEqual(parsed.sql(), "SELECT 1")


def test_only_the_independent_harness_can_be_replayed():
    owners = {_Example: ("validate_identity",)}
    assert admissible(_Example, "test_independent_calls", owners)
    assert not admissible(_Example, "test_state_crosses_calls", owners)


def test_direct_assertion_grammar():
    assert safe_body(ast.parse('self.assertEqual(transpile("SELECT 1")[0], "SELECT 1")').body, ())
    assert safe_body(ast.parse('self.assertRaises(ParseError, parse_one, "SELECT ,")').body, ())
    assert safe_body(ast.parse('with self.assertRaises(ParseError): parse_one("SELECT ,")').body, ())
    loops = "cases = {'SELECT 1': 'SELECT 1'}\nfor sql, expected in cases.items():\n"
    loops += "    with self.subTest(f'{sql}'):\n        self.assertEqual(transpile(sql)[0], expected)"
    assert safe_body(ast.parse(loops).body, ())
    assert not safe_body(ast.parse('self.assertEqual(transpile(read_file())[0], "SELECT 1")').body, ())
    assert not safe_body(ast.parse('self.assertEqual(transpile("SELECT 1", **kwargs)[0], "SELECT 1")').body, ())


def test_actual_argument_types_must_survive_replay():
    assert literals(("SELECT 1", {"read": ["postgres", "snowflake"]}))
    assert not literals({"x": lambda: 1})
    assert not literals({1: "sql"})
