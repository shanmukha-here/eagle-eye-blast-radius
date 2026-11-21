import dis
import unittest
from decimal import Decimal

from localboard import historical_forecast
from observed import Observers, enum_edit, plain_enums
from reach import nested_edit, witnessed_bypass


class Observations(unittest.TestCase):
    source = "class Kind(AutoName):\n    A = auto()\n    B = 'bee'\n"

    def test_enum_lookup_both_values_and_generic_observers(self):
        prefix = "enum:module.py:Kind:"
        obs = Observers({prefix + k: hex(v) for k, v in
                         [("defined", 63), ("unknown", 1), ("value:B", 2),
                          ("lookup:bee", 4), ("lookup:other", 8)]})
        self.assertEqual(obs.users("module.py", "Kind", "B", self.source,
                                   self.source.replace("bee", "other")), 15)
        self.assertIsNone(obs.users("module.py", "Kind", "B", self.source,
                                    self.source.replace("bee", "A")))

    def test_enum_rejects_alias_mixed_change_and_custom_constructor(self):
        replacement = self.source.replace("auto()", "'other'")
        self.assertEqual(enum_edit(self.source, replacement), [("Kind", "A")])
        self.assertEqual(enum_edit(self.source, replacement + "x = True\n"), [])
        self.assertFalse(plain_enums(self.source.replace("bee", "A")))
        self.assertFalse(plain_enums(self.source + "    def __new__(cls): pass\n"))

    def test_component_requires_recorded_declaration(self):
        source = "class Parser:\n    FLAG = True\n"
        changed = source.replace("True", "False")
        self.assertIsNone(Observers({}).users("f", "Parser", "FLAG", source, changed))
        obs = Observers({"component:f:Parser:defined:FLAG": "1"})
        self.assertEqual(obs.users("f", "Parser", "FLAG", source, changed), 0)
        referred = source + "    COPY = FLAG\n"
        self.assertIsNone(obs.users("f", "Parser", "FLAG", referred, referred.replace("True", "False")))

    def test_unchanged_factory_can_isolate_its_nested_body(self):
        text = "def factory(x):\n def f(a):\n  return a + 2\n return f\n"
        def code(s):
            return compile(s, "m.py", "exec").co_consts[0]
        a, b = code(text), code(text.replace("+ 2", "- 2"))
        self.assertEqual(nested_edit(a, b)[0].co_name, "f")
        self.assertEqual(nested_edit(a, code(text.replace("return f", "return x")))[0], a)

    def test_bypass_needs_positive_branch_and_independent_line(self):
        code = compile("def f(x):\n if x:\n  return 3\n return 5\n", "m.py", "exec").co_consts[0]
        ins = list(dis.get_instructions(code))
        off = next(i.offset for i in ins if i.argval == 3)
        self.assertIsNone(witnessed_bypass(code, off, lambda _: False, {}))
        self.assertEqual(witnessed_bypass(code, off, lambda _: True, {3: 7}), 7)

    def test_forecast_anchors_and_discount(self):
        knots = [["100", "105"], ["200", "210"]]
        self.assertEqual(historical_forecast(Decimal(100), knots, Decimal(".3")), Decimal(105))
        self.assertEqual(historical_forecast(Decimal(50), knots, Decimal(".3")), Decimal(90))
        self.assertEqual(historical_forecast(Decimal(150), knots, Decimal(".3")), Decimal("157.5"))
