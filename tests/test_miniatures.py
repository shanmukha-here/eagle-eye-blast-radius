import ast
import unittest

from startup import cached_return
from stress_guard import eligible


def body(text):
    return ast.parse(text)


class ProbeGuards(unittest.TestCase):
    def test_parser_inputs(self):
        a = body("return self._match(TokenType.FROM)")
        self.assertTrue(eligible("sqlglot/parser.py", a, body("return self._match(TokenType.VAR)")))
        for changed in ("return False", "return len(tokens) > 10000", "return self._match(other.TokenType.FROM)"):
            self.assertFalse(eligible("sqlglot/parser.py", a, body(changed)))

    def test_scaling_guard(self):
        a = "if self._max_nodes:\n    return self._match(TokenType.FROM)"
        self.assertFalse(eligible("sqlglot/parser.py", body(a), body(a.replace("FROM", "VAR"))))
        self.assertFalse(eligible("sqlglot/tokenizer_core.py", body("return True"), body("return False")))

    def test_cache_written_before_return(self):
        a = body("globals()[name] = result\nreturn result")
        self.assertTrue(cached_return(a, body("globals()[name] = result\nreturn None")))
        self.assertFalse(cached_return(a, body("globals()[name] = None\nreturn None")))
        self.assertFalse(cached_return(a, body("globals()[name] = result\nreturn fn()")))

    def test_core_edit_needs_full_width_witness(self):
        from impact import structure
        from miniatures import Miniatures

        probes = Miniatures.__new__(Miniatures)
        probes.tests = {"parser": 4}
        old = structure(body("def parse(sql):\n    return sql"))
        new = structure(body("def parse(sql):\n    return sql[:100]"))
        self.assertEqual(probes.protect_core(1, None, "sqlglot/tokens.py", "", old, new), 5)
        self.assertEqual(probes.protect_core(1, None, "sqlglot/tokens.py", "", old, old), 1)
