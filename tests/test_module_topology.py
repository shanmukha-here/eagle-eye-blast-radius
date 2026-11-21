import difflib
from pathlib import Path

import pytest

from module_topology import addition, hard_topology, opaque_format


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


def test_unsupported_patch_encodings_are_conservative():
    assert opaque_format("diff --git a/fixture b/fixture\nGIT binary patch\nliteral 8\n")
    assert opaque_format("@@ -1 +1 @@\n-select 1\n+select 1\r\n")
    assert opaque_format("+value\n" * 1001)
    assert not opaque_format("@@ -1 +1 @@\n-select 1\n+select 2\n")
