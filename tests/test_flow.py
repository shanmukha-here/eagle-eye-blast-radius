import ast
import dis
import unittest
from types import SimpleNamespace

from checkpoint import table_digest
from flow import Flow, changed_offsets
from optimizer_probe import local_edit


def function(source):
    code = compile("def f(x):\n" + source, "example.py", "exec")
    return code.co_consts[0]


class ControlFlowTests(unittest.TestCase):
    def test_only_changed_branch_is_a_frontier(self):
        body = "    if x:\n        return 17\n    return 23\n"
        a = function(body)
        b = function(body.replace("17", "31"))
        changed = changed_offsets(a, b)
        original = {i.offset: i for i in dis.get_instructions(a)}
        self.assertEqual([original[i].argval for i in changed], [17])
        self.assertEqual(changed_offsets(a, a), set())

    def test_unmatched_frame_and_exception_handler_widen(self):
        a = function("    return x\n")
        self.assertIsNone(changed_offsets(a, function("    y = x\n    return y\n")))
        body = "    try:\n        return x + 2\n    except ValueError:\n        return 1\n"
        self.assertIsNone(changed_offsets(function(body), function(body.replace("2", "3"))))

    def test_comparison_proof_requires_observation_and_isolated_edit(self):
        flow = Flow({"code": {}, "pool": [], "opaque": "1", "reflect": "2",
                     "types": {"f:1:0:0": ["builtin:int"]}, "files": []})
        self.assertEqual(flow.opaque, 3)
        self.assertTrue(flow.equality("f", ast.parse("x is None"), ast.parse("x == None")))
        self.assertFalse(flow.equality("f", ast.parse("x is None"), ast.parse("y == None")))
        self.assertFalse(flow.equality("unknown", ast.parse("x is None"), ast.parse("x == None")))

    def test_new_call_or_state_write_disables_query_replay(self):
        old = ast.parse("return f(x)")
        self.assertTrue(local_edit(old, ast.parse("return f(x + 1)")))
        self.assertFalse(local_edit(old, ast.parse("return g(x)")))
        self.assertFalse(local_edit(old, ast.parse("x.value = 1\nreturn f(x)")))

    def test_table_fingerprint_preserves_order_and_literal_types(self):
        def result(rows):
            return table_digest(SimpleNamespace(columns=("a",), rows=rows))
        self.assertNotEqual(result([(1,), (2,)]), result([(2,), (1,)]))
        self.assertNotEqual(result([(1,)]), result([(True,)]))
        with self.assertRaises(TypeError):
            result([(object(),)])
