from registry_guard import bindings


def test_interior_dispatch_key_diff_is_observed():
    source = "class Translator:\n    FUNCTIONS = {\n        'first': lambda x: x,\n        'second': int,\n    }\n"
    path = "sqlglot/translator.py"
    assert bindings(path, source) != bindings(path, source.replace("'second': int", "'second': float"))


def test_late_import_routing_guard_observes_changed_local():
    source = "def lookup(key):\n    key = key.lower()\n    return import_module(f'pkg.{key}')\n"
    path = "sqlglot/example.py"
    assert bindings(path, source) != bindings(path, source.replace("lower()", "upper()"))


def test_ordinary_code_is_not_a_dispatch_table():
    assert bindings("sqlglot/example.py", "def calculate(number):\n    return number + 1\n") == ()
