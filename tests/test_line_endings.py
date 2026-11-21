from types import SimpleNamespace

from impact import patched, split_diff, UnknownChange
from surface import Surface


def conversion(path, before, after):
    return (f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n"
            f"@@ -1 +1 @@\n-{before}\n+{after}\r\n")


def test_physical_patch_requires_original_line_ending():
    path = "sqlglot/sample.py"
    edits = split_diff(conversion(path, "BASE = 1", "BASE = 1"), physical=True)
    assert patched("BASE = 1\n", edits[path], physical=True) == "BASE = 1\r\n"
    try:
        patched("BASE = 1\r\n", edits[path], physical=True)
    except UnknownChange:
        return
    raise AssertionError("used a normalized context to apply a physical hunk")


def test_line_ending_proof_keeps_fixture_readers(tmp_path):
    source = "sqlglot/sample.py"
    fixture = "tests/fixtures/sample.sql"
    for path, data in ((source, "BASE = 1\n"), (fixture, "SELECT 1\n")):
        file = tmp_path / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(data)
    details = SimpleNamespace(lines={source: {}}, positions=2, flow=SimpleNamespace(opaque=1))
    impact = SimpleNamespace(root=tmp_path, details=details, files={fixture: {0: 4}})
    surface = Surface(impact, tmp_path, {})
    surface.index = SimpleNamespace(get=lambda _: ())
    assert surface.line_endings(conversion(source, "BASE = 1", "BASE = 1")) == 3
    assert surface.line_endings(conversion(source, "BASE = 1", "BASE = 2")) is None
    assert surface.line_endings(conversion(fixture, "SELECT 1", "SELECT 2")) == 5
