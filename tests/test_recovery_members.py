import ast
from types import SimpleNamespace

from recovery import Recovery


PATH = "sqlglot/example.py"
OWNER = "Example"
BASE = """\
from __future__ import annotations

class Example:
    MODE = "one"

    def helper(self, flag: bool = False) -> str:
        return "helper"

    def result(self):
        return self.MODE
"""


def recovery(source):
    selector = Recovery.__new__(Recovery)
    selector.imports = SimpleNamespace(mask=lambda key: 1)
    selector.receivers = SimpleNamespace(bits=lambda key: 2)
    selector.impact = SimpleNamespace(
        base=lambda path: (source, None, None),
        files={PATH: {0: 4}},
        details=SimpleNamespace(access=SimpleNamespace(seen={}),
                                flow=SimpleNamespace(opaque=8, reflect=16)),
    )
    return selector


def without_helper(source):
    return source.replace('    def helper(self, flag: bool = False) -> str:\n        return "helper"\n\n', "")


def test_isolated_removal_keeps_named_and_reflective_observers():
    selector = recovery(BASE)
    after = ast.parse(without_helper(BASE))
    assert selector.removed_method(PATH, after, OWNER, "helper") == 31


def test_removal_with_a_class_alias_or_body_change_keeps_the_broad_rule():
    alias = BASE + "\n    alias = helper\n"
    for source, revised in ((alias, without_helper(alias)),
                            (BASE, without_helper(BASE).replace("self.MODE", "None"))):
        assert recovery(source).removed_method(PATH, ast.parse(revised), OWNER, "helper") is None


def test_literal_attribute_requires_an_unchanged_class_namespace():
    changed = BASE.replace('"one"', '"two"')
    assert recovery(BASE).literal_attribute(PATH, ast.parse(changed), changed, OWNER, "MODE") == 31
    alias = BASE + "\n    alias = MODE\n"
    changed = alias.replace('"one"', '"two"')
    assert recovery(alias).literal_attribute(PATH, ast.parse(changed), changed, OWNER, "MODE") is None
