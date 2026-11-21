import ast

from precision_body import body_regions, local_bindings
from precision_index import PrecisionIndex, build
from registry_keys import one_entry


def test_dense_index_reads_exact_bits(tmp_path):
    path = tmp_path / "precision.index"
    build(path, {"L:mod:1": 2**513 | 3, "F:mod:8": 1}, 96)
    index = PrecisionIndex(path)
    assert index.mask("L:mod:1") == 2**513 | 3
    assert index.mask("F:mod:8") == 1
    assert index.mask("B:mod:missing") == 0


def test_body_control_and_insertion_are_retained():
    original = ast.parse("if value > 2:\n    finish(value)\nelse:\n    skip(value)\n")
    revised = ast.parse("if value >= 2:\n    begin(value)\n    finish(value)\nelse:\n    skip(value)\n")
    changed, added, condition = body_regions(original, revised)
    assert not changed
    assert added == {(1, 1), (2, 2)}
    assert condition == {(1, 2)}


def test_dispatch_accepts_only_one_stable_slot(tmp_path):
    path = "sqlglot/example.py"
    source = "class Parent:\n    FUNCTIONS = {'a': lambda x: x + 3, 'b': float}\n"
    assert one_entry(tmp_path, path, source, source.replace("x + 3", "x + 4")) == ("Parent.FUNCTIONS", "a")
    assert one_entry(tmp_path, path, source, source.replace("'a'", "'z'")) is None
    assert one_entry(tmp_path, path, source, source.replace("x + 3", "x + 4") + "\nsetup()\n") is None


def test_dispatch_factory_only_builds_a_closure(tmp_path):
    source = ("def factory(flag):\n    def handler(state):\n        return state if flag else None\n"
              "    return handler\n\nFUNCTIONS = {'a': factory(True)}\n")
    revised = source.replace("factory(True)", "factory(False)")
    assert one_entry(tmp_path, "sqlglot/example.py", source, revised) == ("FUNCTIONS", "a")
    source = source.replace("    return handler", "    activate(flag)\n    return handler")
    revised = source.replace("factory(True)", "factory(False)")
    assert one_entry(tmp_path, "sqlglot/example.py", source, revised) is None


def test_local_bindings_excludes_global_shadowing_and_frame_reads():
    def code(source):
        return compile(source, "example.py", "exec")

    plain = "def fn(value):\n    item = value + 1\n    return item\n"
    assert local_bindings(code(plain), code(plain.replace("item", "result")))
    a = "def fn():\n    print(item)\n    return item\n"
    b = "def fn():\n    print(item)\n    item = 1\n    return item\n"
    assert not local_bindings(code(a), code(b))
    a = "def fn(value):\n    item = value\n    return locals()\n"
    assert not local_bindings(code(a), code(a.replace("item", "result")))
