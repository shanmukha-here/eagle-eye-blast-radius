import os
import sys
import tempfile
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

    def test_qualnames_list_nested_functions(self):
        import zstage
        names = zstage._qualnames("class C:\n    def f(self, x=1):\n        def g():\n            pass\n", "x.py")
        self.assertEqual(names, {"C.f", "C.f.<locals>.g"})


if __name__ == "__main__":
    unittest.main()
