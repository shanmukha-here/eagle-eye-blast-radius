import ast
import unittest

from narrow import fixture_inputs, imports, literal_defaults


class FallbackTests(unittest.TestCase):
    def test_guarded_import_is_part_of_the_graph(self):
        self.assertNotEqual(imports(ast.parse('if False: import io')), imports(ast.parse('if True: import io')))

    def test_expected_side_is_the_only_ignored_fixture_input(self):
        self.assertEqual(fixture_inputs('select 1;X;'), fixture_inputs('select 1;Y;'))
        self.assertNotEqual(fixture_inputs('select 1;X;'), fixture_inputs('select 2;X;'))
        self.assertNotEqual(fixture_inputs('select 1;X;'), fixture_inputs('select 1;X;;'))

    def test_default_requires_the_body_and_decorators_to_match(self):
        src = 'def f(x=True):\n    return x\n'
        self.assertTrue(literal_defaults(src, src.replace('True', 'False')))
        self.assertFalse(literal_defaults(src, src.replace('True', 'False').replace('return x','return 1')))
        self.assertFalse(literal_defaults('@g\n'+src,'@g\n'+src.replace('True','False')))
