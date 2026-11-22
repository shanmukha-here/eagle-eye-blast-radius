import os
import sys
import tempfile
import types
import unittest

import zpatch
import zvalues


class ValueEditTests(unittest.TestCase):
    def analyze(self, old, new):
        return zpatch.analyze("sqlglot/x.py", old, new, "x.py", values=True)

    def test_class_and_module_assignments_are_value_edits(self):
        old = 'A = {"a": 1}\nclass C:\n    T = {"x": 1}\n    def f(self):\n        return self.T\n'
        new = old.replace('"x"', '"x_alt"').replace('"a": 1', '"a": 2')
        r = self.analyze(old, new)
        self.assertEqual(sorted(r["values"]), [("", ["A"]), ("C", ["T"])])
        self.assertEqual(r["changed"], [])

    def test_other_statements_stay_unsupported(self):
        for old, new in (("import os\n", "import sys\n"),
                         ("A = 1\n", "B = 1\n"),
                         ("if X:\n    A = 1\n", "if X:\n    A = 2\n"),
                         ("class C(B):\n    A = 1\n", "class C(D):\n    A = 1\n"),
                         ("f(1)\n", "f(2)\n")):
            with self.assertRaises(zpatch.Unsupported):
                self.analyze(old, new)
        with self.assertRaises(zpatch.Unsupported):
            zpatch.analyze("sqlglot/x.py", "A = 1\n", "A = 2\n", "x.py")

    def test_unordered_digests_ignore_order_only(self):
        loose = zvalues.Digest("/", ordered=False)
        exact = zvalues.Digest("/")
        self.assertEqual(loose((1, 2)), zvalues.Digest("/", ordered=False)((2, 1)))
        self.assertNotEqual(exact((1, 2)), zvalues.Digest("/")((2, 1)))
        self.assertNotEqual(loose((1, 2)), zvalues.Digest("/", ordered=False)((1, 3)))

    def test_digests_see_closures_and_defaults(self):
        def make(v):
            def f(x=v):
                return v
            return f
        self.assertEqual(zvalues.Digest("/")(make(1)), zvalues.Digest("/")(make(1)))
        self.assertNotEqual(zvalues.Digest("/")(make(1)), zvalues.Digest("/")(make(2)))

    def test_overlay_serves_new_text(self):
        with tempfile.TemporaryDirectory(dir=os.path.dirname(os.path.abspath(__file__))) as root:
            os.makedirs(os.path.join(root, "pkgz"))
            with open(os.path.join(root, "pkgz", "__init__.py"), "w") as f:
                f.write("A = 1\n")
            finder = zvalues.Overlay(root, {"pkgz/__init__.py": "A = 2\n"})
            sys.meta_path.insert(0, finder)
            try:
                import pkgz
                self.assertEqual(pkgz.A, 2)
            finally:
                sys.meta_path.remove(finder)
                sys.modules.pop("pkgz", None)

    def test_closure_and_nested_code_changes_are_not_swapped(self):
        old = "class A(B):\n    def f(self, x):\n        return super().f(x)\n    def g(self):\n        return [1]\n"
        new = old.replace("return super().f(x)", "return None").replace("return [1]", "return (lambda: 1)()")
        r = zpatch.analyze("sqlglot/x.py", old, new, "x.py")
        keys = [("sqlglot/x.py", "A.f", 2), ("sqlglot/x.py", "A.g", 4)]
        self.assertEqual(r["changed"], keys)
        self.assertEqual(r["noswap"], keys)
        self.assertEqual(r["swaps"], [])

    def test_qualnames_list_nested_functions(self):
        import zstage
        names = zstage._qualnames("class C:\n    def f(self, x=1):\n        def g():\n            pass\n", "x.py")
        self.assertEqual(names, {"C.f", "C.f.<locals>.g"})

    def test_lookup_sites_split_constant_and_dynamic_names(self):
        import ast
        text = (
            "def f(x, name, vars):\n"
            "    getattr(x, 'copy_options')\n"
            "    hasattr(x, name)\n"
            "    getattr(x, f'{name}_sql')\n"
            "    x.__dict__.get('Parser')\n"
            "    x.eval(name)\n"
            "    print(vars)\n"
            "    return '{0.walk}'.format(x), dir(x)\n"
        )
        sites = [(n.lineno, w, s) for n, w, s in zpatch.lookup_sites(ast.parse(text))]
        self.assertIn((2, {"copy_options"}, ""), sites)
        self.assertIn((3, None, ""), sites)
        self.assertIn((4, None, "_sql"), sites)
        self.assertIn((5, {"Parser"}, ""), sites)
        self.assertIn((8, {"walk"}, ""), sites)
        self.assertIn((8, None, ""), sites)
        self.assertFalse(any(line in (6, 7) for line, _, _ in sites))

    def test_member_reach_masks_lookups_and_declines_new_ones(self):
        import zstage
        c = zstage.Candidates.__new__(zstage.Candidates)
        c.classes = {"unique": {("sqlglot/x.py", "A"): 0, ("sqlglot/x.py", "E"): 1},
                     "classes": [(("sqlglot/x.py", "A"), [0, "builtins.object"], {}),
                                 (("sqlglot/x.py", "E"), [1, "builtins.Exception", "builtins.object"], {})]}
        c.sinks = {"attrs": {"n": [("sqlglot/y.py", "g", 3, 4, False), ("sqlglot/y.py", "<module>", 1, 9, False),
                                   ("sqlglot/y.py", "g", 3, 5, True)]},
                   "constants": {"k"}, "dynamic": [("sqlglot/z.py", 7, "_sql", [("sqlglot/z.py", "h", 6)]),
                                                   ("sqlglot/z.py", 8, "", [("sqlglot/z.py", "h", 6)])]}
        c.preload = {("sqlglot/y.py", "<module>", 1)}
        c.lines = {"sqlglot/y.py": {4: 0b10, 5: 0b10000}, "sqlglot/z.py": {8: 0b1000}}
        c.masks = {("sqlglot/z.py", "h", 6): 0b100}
        old = "class A:\n    pass\n"
        results = {"sqlglot/x.py": {"old_text": old, "changed": []}}
        keys, mask = c._reach("sqlglot/x.py", "A", "n", results, {"sqlglot/x.py": old})
        self.assertEqual(keys, [("sqlglot/y.py", "<module>", 1)])
        self.assertEqual(mask, 0b11010)
        _, mask = c._reach("sqlglot/x.py", "A", "n", results, {"sqlglot/x.py": old}, calls=False)
        self.assertEqual(mask, 0b1010)
        self.assertIsNone(c._reach("sqlglot/x.py", "A", "k", results, {"sqlglot/x.py": old}))
        self.assertIsNone(c._reach("sqlglot/x.py", "E", "n", results, {"sqlglot/x.py": old}))
        self.assertIsNone(c._reach("sqlglot/x.py", "A", "close", results, {"sqlglot/x.py": old}))
        # A new lookup in the patched file, outside changed code, is not proven.
        new = old + "\ndef q(o):\n    return getattr(o, 'n')\n"
        self.assertIsNone(c._reach("sqlglot/x.py", "A", "n", results, {"sqlglot/x.py": new}))
        new = old + "\ndef q(o):\n    return o.n\n"
        self.assertIsNone(c._reach("sqlglot/x.py", "A", "n", results, {"sqlglot/x.py": new}))
        results["sqlglot/x.py"]["changed"] = [("sqlglot/x.py", "q", 4)]
        self.assertIsNotNone(c._reach("sqlglot/x.py", "A", "n", results, {"sqlglot/x.py": new}))

    def test_fixture_items_and_cases_follow_the_pinned_loaders(self):
        import zstage
        # The two loaders of the pinned tests/helpers.py.
        helpers = (
            "import os\nFILE_DIR = os.path.dirname(__file__)\n"
            "FIXTURES_DIR = os.path.join(FILE_DIR, 'fixtures')\n"
            "def _filter_comments(s):\n"
            "    return '\\n'.join([line for line in s.splitlines() if line and not line.startswith('--')])\n"
            "def load_sql_fixtures(filename):\n"
            "    with open(os.path.join(FIXTURES_DIR, filename), encoding='utf-8') as f:\n"
            "        lines = []\n"
            "        for line in _filter_comments(f.read()).splitlines():\n"
            "            lines.append(line)\n"
            "            if not line.startswith('#'):\n"
            "                yield '\\n'.join(lines)\n"
            "                lines.clear()\n"
            "def load_sql_fixture_pairs(filename):\n"
            "    with open(os.path.join(FIXTURES_DIR, filename), encoding='utf-8') as f:\n"
            "        statements = _filter_comments(f.read()).split(';')\n"
            "        for i in range(0, len(statements), 2):\n"
            "            if i + 1 < len(statements):\n"
            "                yield {}, statements[i].strip(), statements[i + 1].strip()\n")
        with tempfile.TemporaryDirectory(dir=os.path.dirname(os.path.abspath(__file__))) as root:
            os.makedirs(os.path.join(root, "tests", "fixtures"))
            with open(os.path.join(root, "tests", "helpers.py"), "w") as f:
                f.write(helpers)
            old = "SELECT 1;\n1;\n-- note\nSELECT 2;\n2;\n"
            with open(os.path.join(root, "tests", "fixtures", "a.sql"), "w") as f:
                f.write(old)
            c = zstage.Candidates.__new__(zstage.Candidates)
            c.base, c.helpers = zstage.Path(root), None
            found = c.fixture_items("tests/fixtures/a.sql", old.replace("SELECT 2", "SELECT 3"))
            self.assertEqual(found, {"load_sql_fixture_pairs": ({1}, 2), "load_sql_fixtures": ({2}, 4)})
            # Comments are not items; an added item changes the count, so no loader is offered.
            self.assertEqual(c.fixture_items("tests/fixtures/a.sql", old.replace("note", "other"))
                             ["load_sql_fixture_pairs"], (set(), 2))
            self.assertEqual(c.fixture_items("tests/fixtures/a.sql", old + "SELECT 4;\n4;\n"), {})
        r = zstage.Execute.__new__(zstage.Execute)
        name = "tests/test_serde.py::TestSerde::test_serde"
        r.swap = types.SimpleNamespace(ids=["x", name],
                                       fixtures={"opens": {"tests/fixtures/a.sql": 0b10}})
        record = {"cases": [("a.sql", 0), ("a.sql", 1), ("a.sql", 2), ("a.sql", 3)]}
        items = {"a.sql": {"load_sql_fixtures": ({3}, 4)}}
        self.assertEqual(r._fixture_cases(1, record, items), 0b1000)
        self.assertIsNone(r._fixture_cases(1, record, {"a.sql": {"load_sql_fixtures": ({3}, 5)}}))
        self.assertIsNone(r._fixture_cases(1, record, {"a.sql": {"load_sql_fixture_pairs": ({1}, 2)}}))
        # A file the test neither opens nor loads leaves it unaffected.
        self.assertEqual(r._fixture_cases(1, record, {"b.sql": {"load_sql_fixtures": ({0}, 1)}}), 0)
        r.swap.ids = ["x", "tests/test_executor.py::TestExecutor::test_execute_tpch"]
        self.assertIsNone(r._fixture_cases(1, record, items))
        r.swap.ids = ["x", name]
        r.swap.fixtures = {"opens": {"tests/fixtures/a.sql": 0b01}}
        self.assertIsNone(r._fixture_cases(1, record, items))

    def test_zygote_serves_fixtures_to_the_helper_loaders_only(self):
        import zexec
        with tempfile.TemporaryDirectory(dir=os.path.dirname(os.path.abspath(__file__))) as root:
            os.makedirs(os.path.join(root, "tests", "fixtures"))
            path = os.path.join(root, "tests", "fixtures", "a.sql")
            with open(path, "w") as f:
                f.write("old\n")
            module = types.ModuleType("fake_helpers")
            module.__file__ = os.path.join(root, "tests", "helpers.py")
            sys.modules["fake_helpers"] = module
            try:
                zexec._serve_fixtures(root, {os.path.abspath(path): "new\n"})
                with module.open(path, encoding="utf-8") as f:
                    self.assertEqual(f.read(), "new\n")
                with open(path) as f:
                    self.assertEqual(f.read(), "old\n")
                with self.assertRaises(OSError):
                    module.open(path, "rb")
            finally:
                del sys.modules["fake_helpers"]
            with self.assertRaises(RuntimeError):
                zexec._serve_fixtures(root, {path: "new\n"})

    def test_plain_removed_lists_undecorated_class_methods(self):
        old = ("class A:\n    def f(self):\n        return 1\n    def h(self):\n        return 3\n"
               "def m():\n    return 4\n")
        new = "class A:\n    def h(self):\n        return 3\n"
        r = zpatch.analyze("sqlglot/x.py", old, new, "x.py")
        self.assertEqual(sorted(name for _, name, _ in r["removed"]), ["f", "m"])
        # Module functions are not deleted in place.
        self.assertEqual(r["plain_removed"], [("A", "f")])

    def test_zygote_deletes_a_method_only_when_nothing_else_holds_it(self):
        import gc
        import zexec
        base = os.path.dirname(os.path.abspath(__file__))
        filename = os.path.join(base, "sqlglot", "x.py")
        source = "class A:\n    def f(self):\n        return 1\nclass B(A):\n    pass\n"
        for alias in (False, True):
            module = types.ModuleType("x")
            exec(compile(source, filename, "exec"), module.__dict__)
            func = module.A.f
            index = {("sqlglot/x.py", "A.f", 2): [func]}
            if alias:
                module.CACHE = {"f": func}
            frozen = gc.get_freeze_count()
            try:
                zexec._delete(base, module, "A", "f", ("sqlglot/x.py", "A.f", 2), index)
                self.assertFalse(alias)
                self.assertFalse(hasattr(module.B(), "f"))
            except LookupError:
                self.assertTrue(alias)
            self.assertEqual(gc.get_freeze_count(), max(frozen, gc.get_freeze_count()))
            gc.unfreeze()
        module = types.ModuleType("x")
        exec(compile(source, filename, "exec"), module.__dict__)
        with self.assertRaises(LookupError):
            zexec._delete(base, module, "A", "f", ("sqlglot/x.py", "A.f", 9), {})


if __name__ == "__main__":
    unittest.main()
