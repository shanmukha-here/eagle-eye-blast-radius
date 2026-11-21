from warm_import import WarmImport


def test_warm_import_checks_only_modules_outside_the_tracked_prefix(tmp_path):
    package = tmp_path / "sqlglot"
    dialects = package / "dialects"
    dialects.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "optional.py").write_text("raise RuntimeError('optional module unavailable')\n")
    (package / "later.py").write_text("value = 0\n")
    (dialects / "__init__.py").write_text(
        "try:\n"
        "    import sqlglot.optional\n"
        "except RuntimeError:\n"
        "    pass\n"
        "class Dialect:\n"
        "    classes = {}\n"
    )
    probe = WarmImport(tmp_path)
    try:
        assert probe("sqlglot/dialects/__init__.py", "raise ValueError\n") is None
        assert probe("sqlglot/optional.py", "value = 1\n") is None
        assert probe("sqlglot/later.py", "value = 2\n") is True
        assert probe("sqlglot/later.py", "raise ValueError\n") is False
        assert probe.served == 2
    finally:
        probe.close()
