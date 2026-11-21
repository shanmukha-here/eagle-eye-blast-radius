import ast

import pytest

from receiver_edits import declaration


@pytest.mark.parametrize("before,after,member", [
    ("    x = 1\n", "    x = 1\n    def render(self): return 1\n", "render"),
    ("    x = 1\n    def render(self): return 1\n", "    x = 1\n", "render"),
    ("    def f(self, x=1): return x\n", "    def f(self, x=2): return x\n", "f"),
    ("    label = 'old'\n", "    label = 'new'\n", "label"),
])
def test_only_one_declaration_changes(before, after, member):
    a = ast.parse("class Owner:\n" + before)
    b = ast.parse("class Owner:\n" + after)
    assert declaration(a, b) == ("Owner", member)


def test_dispatch_change_and_second_edit_are_not_attributed_to_a_plain_member():
    a = ast.parse("class Owner:\n    label = 'old'\n    TRANSFORMS = {}\n")
    b = ast.parse("class Owner:\n    label = 'new'\n    TRANSFORMS = {1: object()}\n")
    assert declaration(a, b) is None
    b = ast.parse("class Owner:\n    label = 'new'\n    TRANSFORMS = {}\nimport os\n")
    assert declaration(a, b) is None
