import tempfile
import unittest
from pathlib import Path

from dependencies import (
    Dependencies,
    appended_failure,
    name_bits,
    scalar_declarations,
    simple_collection,
)
from impact import new_source


class SourceAdditions(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "sqlglot").mkdir()
        names = bytearray(32)
        for i in name_bits(b"used", len(names)):
            names[i // 8] |= 1 << (i % 8)
        self.deps = Dependencies(
            {
                "events": {
                    "dir:sqlglot": "1",
                    "path:novel.py": "2",
                    "module:sqlglot.novel": "4",
                },
                "identifiers": names.hex(),
            }
        )

    def test_inert_addition_keeps_discovery_and_lookup_users(self):
        self.assertEqual(
            self.deps.addition(self.root, "sqlglot/novel.py", "VALUE = 3", 15), 7
        )

    def test_known_names_test_names_packages_and_side_effects_widen(self):
        for p, src in [
            ("sqlglot/used.py", ""),
            ("sqlglot/test_novel.py", ""),
            ("sqlglot/typing/novel.py", ""),
            ("sqlglot/novel.py", "register()"),
            ("sqlglot/novel.py", "__all__ = 'item'"),
        ]:
            self.assertEqual(self.deps.addition(self.root, p, src, 15), 15)
        (self.root / "sqlglot/novel").mkdir()
        self.assertEqual(self.deps.addition(self.root, "sqlglot/novel.py", "", 15), 15)

    def test_ordinary_new_file_and_disallowed_multi_file_diff(self):
        patch = (
            "diff --git a/sqlglot/novel.py b/sqlglot/novel.py\nnew file mode 100644\n"
            "index 0000000..aaa1111\n--- /dev/null\n+++ b/sqlglot/novel.py\n"
            "@@ -0,0 +1 @@\n+item = 1\n"
        )
        self.assertEqual(new_source(patch), ("sqlglot/novel.py", "item = 1\n"))
        for bad in [
            patch + patch,
            patch.replace("-0,0", "-1,0"),
            patch.replace("novel.py", "../novel.py"),
        ]:
            self.assertIsNone(new_source(bad))


class TestCollectionAndFields(unittest.TestCase):
    def test_appended_collection_exception_is_narrowly_identified(self):
        base = "class TestOne:\n    def test_one(self): pass\n"
        self.assertTrue(appended_failure(base, base + "raise ImportError('failure')\n"))
        self.assertTrue(
            appended_failure(base, base + "from sys import _no_such_attr_9871\n")
        )
        for tail in [
            "raise ValueError(mutate())",
            "raise Nope()",
            "import os\nos.environ.clear()",
        ]:
            self.assertFalse(appended_failure(base, base + tail))
        shadow = "ValueError = callback\n"
        self.assertFalse(appended_failure(shadow, shadow + "raise ValueError()"))

    def test_collection_side_effects_keep_the_wide_fallback(self):
        self.assertTrue(
            simple_collection(
                "import unittest\nclass TestA(unittest.TestCase):\n    def test_a(self): pass"
            )
        )
        self.assertFalse(simple_collection("init()\nclass TestA: pass"))
        self.assertFalse(
            simple_collection("class TestA:\n    def test_a(self, x=init()): pass")
        )

    def test_scalar_refinement_excludes_derived_fields_and_containers(self):
        src = "class A:\n    ONE = -1\n    TWO = 2\n    TABLE = {1: TWO}\n"
        self.assertEqual(scalar_declarations(src), {("A", "ONE")})
