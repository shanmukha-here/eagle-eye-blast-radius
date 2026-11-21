import ast

from definition_metadata import function_docs, future_types
from tools.build_project_readers import parameter_names, standard_names


def test_function_doc_update_preserves_runtime_shape():
    old = ast.parse('def calculate():\n    """Before."""\n    return 1\n')
    new = ast.parse('def calculate():\n    """After."""\n    return 1\n')
    assert function_docs(old, new)
    assert ast.get_docstring(new.body[0]) == "After."
    assert not function_docs(old, ast.parse('def calculate():\n    """After."""\n    return 2\n'))


def test_doctest_and_class_doc_updates_keep_broad_scope():
    a = ast.parse('def calculate():\n    """>>> calculate()\n    1"""\n    return 1\n')
    b = ast.parse('def calculate():\n    """>>> calculate()\n    2"""\n    return 1\n')
    assert not function_docs(a, b)
    assert not function_docs(ast.parse('class A:\n    """Before."""\n'),
                             ast.parse('class A:\n    """After."""\n'))


def test_methods_and_uncollected_doctests_can_change_their_docs():
    old = ast.parse('class A:\n    def f(self):\n        """>>> f()"""\n        return 0\n')
    new = ast.parse('class A:\n    def f(self):\n        return 0\n')
    assert not function_docs(old, new)
    assert function_docs(old, new, doctests=False)
    assert not function_docs(old, ast.parse("class A:\n    def f(self):\n        return 1\n"), doctests=False)


def test_only_postponed_annotation_values_are_ignored():
    old = "def f(value: str) -> str:\n    return value\n"
    new = old.replace("str", "Other")
    future = "from __future__ import annotations\n"
    assert not future_types(ast.parse(old), ast.parse(new))
    assert future_types(ast.parse(future + old), ast.parse(future + new))
    assert not future_types(ast.parse(future + old), ast.parse(future + new.replace("return value", "return None")))


def test_signature_keys_do_not_expose_annotation_values():
    def recognized(text):
        tree = ast.parse("import inspect\n" + text)
        nodes = list(ast.walk(tree))
        parents = {child: node for node in nodes for child in ast.iter_child_nodes(node)}
        member, = [node for node in nodes if isinstance(node, ast.Attribute) and node.attr == "signature"]
        return standard_names(tree, nodes) and parameter_names(member, parents)

    names = "[name for _, name in enumerate(inspect.signature(fn).parameters)]\n"
    assert recognized(names)
    assert not recognized("enumerate = callback\n" + names)
    assert not recognized(names.replace("signature(fn)", "signature(fn, eval_str=True)"))
    assert not recognized(names.replace(".parameters", ".parameters.values()"))
    assert not recognized("sample = inspect.signature(fn).parameters\n")
