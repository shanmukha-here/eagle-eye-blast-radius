import ast
import difflib
from pathlib import Path

import pytest

from decision import overlay
from module_topology import addition, hard_topology
from tools.build_project_readers import importer_masks


@pytest.mark.parametrize("module", ["sqlglot/dialects/snowflake.py", "sqlglot/generators/teradata.py"])
def test_module_removal_requires_conservative_selection(module):
    base = Path("/base") / module
    original = base.read_text() if base.is_file() else "from sqlglot import exp\n"
    hunks = difflib.unified_diff(original.splitlines(keepends=True), [], f"a/{module}", "/dev/null")
    deletion = f"diff --git a/{module} b/{module}\ndeleted file mode 100644\n" + "".join(hunks)
    assert hard_topology(deletion)


def test_module_rename_and_shadowing_are_topology_changes():
    rename = "diff --git a/sqlglot/a.py b/sqlglot/b.py\nrename from sqlglot/a.py\nrename to sqlglot/b.py\n"
    shadow = "diff --git a/sqlglot/a.py b/sqlglot/a.py\nnew file mode 100644\n--- /dev/null\n+++ b/sqlglot/a.py\n"
    assert hard_topology(rename)
    assert addition(shadow)
    assert not hard_topology(shadow)
    assert not addition(rename)


def test_transitive_importer_index_includes_package_exports():
    trees = {
        "sqlglot/__init__.py": ast.parse(""),
        "sqlglot/dialects/__init__.py": ast.parse(""),
        "sqlglot/dialects/snowflake.py": ast.parse(""),
        "sqlglot/bridge.py": ast.parse("from .dialects import Snowflake"),
        "tests/test_affected.py": ast.parse("from sqlglot.bridge import helper"),
        "tests/test_unrelated.py": ast.parse("from sqlglot import unrelated"),
    }
    readers = importer_masks(trees, {"tests/test_affected.py": 1, "tests/test_unrelated.py": 2})
    assert readers["importers:sqlglot/dialects/snowflake.py"] == ((None, 1),)


def test_removed_module_and_bytecode_stay_absent_from_overlay(tmp_path):
    base, target = tmp_path / "base", tmp_path / "target"
    cached = base / "sqlglot" / "__pycache__"
    cached.mkdir(parents=True)
    (cached.parent / "removed.py").write_text("value = 1\n")
    (cached / "removed.cpython-313.pyc").write_bytes(b"stale")
    (cached / "other.cpython-313.pyc").write_bytes(b"unaffected")
    target.mkdir()
    overlay(base, target, {}, validate=False, remove=("sqlglot/removed.py",))
    assert (cached.parent / "removed.py").is_file()
    assert not (target / "sqlglot/removed.py").exists()
    assert not (target / "sqlglot/__pycache__/removed.cpython-313.pyc").exists()
    assert (target / "sqlglot/__pycache__/other.cpython-313.pyc").read_bytes() == b"unaffected"
