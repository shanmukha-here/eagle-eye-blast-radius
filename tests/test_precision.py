import ast

from precision_body import body_regions, local_bindings
from precision_index import PrecisionIndex, build
from registry_keys import one_entry
from long_gate import LongGate


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


def test_integration_queries_require_execution_evidence(tmp_path):
    from selector import Scheduler

    name = "tests/test_executor.py::TestExecutor::test_execute_tpcds"
    scheduler = Scheduler([{"number": 0, "id": name, "seconds": 21.454}], [name])
    gate = LongGate(scheduler, [name], tmp_path, 60)
    assert gate.apply("synthetic patch", [[0]]) == [[0]]
    assert 0 not in gate.long


def test_nested_scope_short_certificate_checks_import_observations(tmp_path):
    import difflib
    from types import SimpleNamespace

    from impact import Impact
    from precision import Precision

    path = "sqlglot/demo.py"
    before = "def factory():\n    def run():\n        return 1\n    return run\n"
    after = before.replace("return 1", "return 2")
    source = tmp_path / path
    source.parent.mkdir()
    source.write_text(before)
    diff = f"diff --git a/{path} b/{path}\n" + "".join(difflib.unified_diff(
        before.splitlines(True), after.splitlines(True), fromfile="a/" + path, tofile="b/" + path,
    ))
    precision = object.__new__(Precision)
    precision.impact = Impact(tmp_path, {"ids": [], "files": {}, "visitors": {}})
    precision.index = SimpleNamespace(mask={"!import_lines": 1}.get)
    assert precision.short_certificate(diff)
    precision.index = SimpleNamespace(mask={"!import_lines": 1, f"IL:{path}:3": 1}.get)
    assert not precision.short_certificate(diff)


def test_setup_observers_keep_reads_from_changed_callable_bodies(tmp_path):
    import difflib
    from types import SimpleNamespace

    from impact import Impact
    from precision import Precision

    path = "sqlglot/demo.py"
    before = "class Header:\n    setting = False\n\ndef make(enabled=False):\n    return 1\n"
    file = tmp_path / path
    file.parent.mkdir()
    file.write_text(before)

    def patch(after):
        return f"diff --git a/{path} b/{path}\n" + "".join(difflib.unified_diff(
            before.splitlines(True), after.splitlines(True), fromfile="a/" + path, tofile="b/" + path,
        ))

    precision = object.__new__(Precision)
    precision.impact = Impact(tmp_path, {"ids": [], "files": {}, "visitors": {}})
    evidence = {"!import_lines": 1, f"IL:{path}:2": 7, f"IL:{path}:4": 3, f"IL:{path}:5": 6}
    precision.index = SimpleNamespace(mask=lambda key: evidence.get(key, 0))
    assert precision.setup_observers(patch(before.replace("return 1", "return 2"))) == 6
    assert precision.setup_observers(patch(before.replace("setting = False", "setting = True"))) == 0
    assert precision.setup_observers(patch(before.replace("enabled=False", "enabled=True"))) == 3
    assert precision.setup_observers(patch(before.replace("return 1", "return 1  # text"))) == 0
