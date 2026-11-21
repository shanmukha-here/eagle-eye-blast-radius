from difflib import unified_diff
from types import SimpleNamespace

from impact import syntax
from parser_signal import observes_patch


MODULE = "sqlglot/example.py"
FIXTURE = "tests/fixtures/example.sql"
SOURCE = "def calculate():\n    return 1\n"
UPDATED_BODY = SOURCE.replace("return 1", "return 2")
UPDATED_DEFAULT = SOURCE.replace("calculate()", "calculate(n=1)")
PARSER = 1


def change(path, original, revised):
    lines = unified_diff(original.splitlines(keepends=True), revised.splitlines(keepends=True),
                         fromfile=f"a/{path}", tofile=f"b/{path}")
    return f"diff --git a/{path} b/{path}\n" + "".join(lines)


def context(cases=0, *, startup_line=0, file_readers=0, importable=True):
    observations = {"module_coverage": 1, "count:0": 4, f"0:{MODULE}": cases,
                    f"0:{MODULE}:2": startup_line}
    index = SimpleNamespace(mask=lambda key: observations.get(key, 0))
    impact = SimpleNamespace(
        files={FIXTURE: {0: file_readers}},
        base=lambda path: (SOURCE, *syntax(path, SOURCE)[1:]),
        details=SimpleNamespace(import_valid=lambda path, source: importable),
    )
    return impact, index


def test_retains_runtime_use_and_startup_line_edits():
    patch = change(MODULE, SOURCE, UPDATED_BODY)
    assert observes_patch(*context(2), PARSER, patch)
    assert observes_patch(*context(1, startup_line=1), PARSER, patch)
    assert not observes_patch(*context(1), PARSER, patch)


def test_retains_startup_declaration_and_checks_unobserved_imports():
    patch = change(MODULE, SOURCE, UPDATED_DEFAULT)
    assert observes_patch(*context(1), PARSER, patch)
    assert observes_patch(*context(importable=False), PARSER, patch)
    assert not observes_patch(*context(), PARSER, patch)
    imported = change(MODULE, SOURCE, "import re\n" + SOURCE)
    assert observes_patch(*context(), PARSER, imported)


def test_fixture_reader_and_unknown_forms():
    patch = change(FIXTURE, "SELECT 1\n", "SELECT 2\n")
    assert not observes_patch(*context(), PARSER, patch)
    assert observes_patch(*context(file_readers=PARSER), PARSER, patch)
    assert observes_patch(*context(), PARSER, patch.replace(".sql", ".bin"))
    assert observes_patch(*context(), PARSER, patch.replace("\n", "\r\n"))
    oversized = "".join(change(f"sqlglot/module_{i}.py", SOURCE, UPDATED_BODY) for i in range(5))
    modulus = change(MODULE, SOURCE, UPDATED_BODY)
    assert observes_patch(*context(), PARSER, oversized)
    assert not observes_patch(*context(), PARSER, modulus)
