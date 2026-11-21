import ast

from definition_metadata import function_docs


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
