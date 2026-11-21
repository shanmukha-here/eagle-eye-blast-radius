"""Select tracked readers of documentation and build-only configuration."""

import ast
import configparser
from pathlib import Path
import re
import sys
import tomllib

from definition_metadata import equivalent, function_docs, identity_layout
from dependencies import name_bits
from impact import UnknownChange, patched, split_diff, syntax
from observation_file import ObservationFile

PROJECT_TEXT = {"authors", "classifiers", "description", "readme", "urls"}


def ini_sections(text):
    parser = configparser.ConfigParser(interpolation=None)
    parser.read_string(text)
    return {"DEFAULT": dict(parser.defaults()), **{section: dict(parser.items(section))
                                                   for section in parser.sections()}}


def build_configuration(path, before, after):
    """Exclude edits that can change install, imports, or pytest collection."""
    try:
        if path == "setup.cfg":
            previous, revised = ini_sections(before), ini_sections(after)
            return all(section == "mypy" or section.startswith("mypy-")
                       or previous.get(section) == revised.get(section)
                       for section in previous.keys() | revised.keys())
        previous, revised = tomllib.loads(before), tomllib.loads(after)
    except (configparser.Error, ValueError):
        return False
    old_project, new_project = previous.pop("project", {}), revised.pop("project", {})
    if not isinstance(old_project, dict) or not isinstance(new_project, dict):
        return False
    if any(old_project.get(key) != new_project.get(key)
           for key in (old_project.keys() | new_project.keys()) - PROJECT_TEXT):
        return False
    old_tools, new_tools = previous.pop("tool", {}), revised.pop("tool", {})
    if not isinstance(old_tools, dict) or not isinstance(new_tools, dict):
        return False
    # Keep every non-lint tool configuration the same.
    preserved = old_tools.keys() | {"pytest", "setuptools", "setuptools_scm", "uv"}
    return previous == revised and all(old_tools.get(key) == new_tools.get(key)
                                       for key in preserved if key != "ruff")


class Surface:
    def __init__(self, impact, directory, references):
        self.impact = impact
        self.references = references
        filename = directory / "project_readers.index"
        self.index = ObservationFile(filename) if filename.is_file() else None
        self.discovery = self.mask("discover") or 0
        self.introspection = self.mask("introspect") or 0
        self.source_layout = self.mask("source_layout")
        self.function_docs = self.mask("function_docs")

    def mask(self, name):
        rows = self.index.get(name) if self.index is not None else ()
        return rows[0][1] if rows else None

    def module_operation(self, text):
        info = filesystem_change(text, self.impact.root)
        if info is None:
            return None
        old, new, source = info
        details = self.impact.details
        access = getattr(details, "access", None)
        if not access:
            return None
        observed = details.flow.opaque | self.discovery
        if old:
            if not (old in self.impact.files or old in details.lines or old in details.imports):
                return None
            observed |= (details.imports.get(old, 0) | details.dialects.get(old, 0) |
                         self.impact.visitors.get(old, 0) | self.impact.files.get(old, {}).get(0, 0) |
                         self.references.get(old, 0))
        if new:
            directory, _, basename = new.rpartition("/")
            name = Path(new).stem
            if (name.startswith("test") or name.endswith("_test") or name in sys.stdlib_module_names
                    or (self.impact.root / new).exists()
                    or (self.impact.root / new).with_suffix("").exists()
                    or not (self.impact.root / directory / "__init__.py").is_file()
                    or all(access.names[i // 8] & (1 << (i % 8))
                           for i in name_bits(name.encode(), len(access.names)))
                    or not pure_module(source)):
                return None
            observed |= (access.seen.get("module:" + new[:-3].replace("/", "."), 0) |
                         access.seen.get("path:" + basename, 0))
        return observed | access.seen.get("dir:?", 0) | access.seen.get("dir:sqlglot", 0)

    def select(self, text, *, fast=True):
        if self.index is None or text.count("\ndiff --git ") or not text.startswith("diff --git "):
            return None
        try:
            if "\nrename from " in text or "\nnew file mode " in text or "\ndeleted file mode " in text:
                return self.module_operation(text)
            edits = split_diff(text)
            path, = edits
            projected = self.mask("file:" + path)
            if projected is None:
                return self.definition(path, edits[path], text if fast else None)
            original = (self.impact.root / path).read_text()
            modified = patched(original, edits[path])
            if path in ("setup.cfg", "pyproject.toml") and not build_configuration(path, original, modified):
                return None
            if self.impact.details.access:
                basename = path.rpartition("/")[2]
                projected |= self.impact.details.access.seen.get("path:" + basename, 0)
            return projected
        except (UnknownChange, OSError, UnicodeError, ValueError, SyntaxError, RecursionError):
            return None

    def line_endings(self, text):
        """Resolve verified CRLF hunks on pinned Python or tracked SQL fixtures."""
        if self.index is None or "\r" not in text or "\r" in text.replace("\r\n", "\n"):
            return None
        try:
            edits = split_diff(text, physical=True)
            path, = edits
            source = (self.impact.root / path).read_bytes()
            modified = patched(source.decode("utf-8"), edits[path], physical=True).encode("utf-8")
            if source == modified:
                return None
            details = self.impact.details
            readers = self.impact.files.get(path, {}).get(0)
            file_effects = (readers or 0) | (self.mask("file:" + path) or 0)
            opaque = details.flow.opaque
            if path.startswith("tests/fixtures/") and path.endswith(".sql") and readers:
                return file_effects | opaque | self.references.get(path, 0)
            if not path.startswith("sqlglot/") or not path.endswith(".py") or path.endswith("/__init__.py"):
                return None
            if path not in details.lines:
                return None
            before, after = (compile(content, path, "exec", flags=ast.PyCF_ONLY_AST, dont_inherit=True)
                             for content in (source, modified))
            if ast.dump(before) == ast.dump(after):
                layout = self.source_layout if self.source_layout is not None else self.introspection
                return file_effects | opaque | details.positions | layout
        except (UnknownChange, OSError, UnicodeError, ValueError, SyntaxError, RecursionError, LookupError):
            pass
        return None

    def definition(self, path, hunks, raw):
        if not path.startswith("sqlglot/") or not path.endswith(".py"):
            return None
        # Guard the additional AST walk on the changed lines alone.
        if raw is not None:
            signs = [line[1:].lstrip() for line in raw.splitlines()
                     if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))]
            if not any(line.startswith(("'", '"', "@", "from ")) or " -> " in line for line in signs):
                return None
        original = (self.impact.root / path).read_text()
        modified = patched(original, hunks)
        old, (outer, scopes), _ = syntax(path, original)
        new, (new_outer, new_scopes), _ = syntax(path, modified)
        if outer == new_outer and scopes.keys() == new_scopes.keys() and all(
            scopes[key][2] == new_scopes[key][2] for key in scopes
        ):
            return None
        access = self.impact.details.access
        if (self.source_layout is not None and ("@(" in original or "@(" in modified)
                and identity_layout(original, modified)):
            return (self.impact.details.positions | self.impact.details.flow.opaque |
                    self.impact.files.get(path, {}).get(0, 0) | self.source_layout)
        if (self.source_layout is not None and self.function_docs is not None
                and not path.endswith("/__init__.py") and function_docs(old, new)):
            return (self.impact.details.positions | self.impact.details.flow.opaque |
                    self.impact.files.get(path, {}).get(0, 0) | self.source_layout | self.function_docs)
        if not equivalent(path, original, modified, old, new, access.names if access else b""):
            return None
        return (self.impact.details.positions | self.impact.details.flow.opaque |
                self.impact.files.get(path, {}).get(0, 0) | self.introspection)


def pure_module(source):
    """Allow definitions whose import installs only bindings in the new module."""
    tree = ast.parse(source)
    deferred = any(isinstance(node, ast.ImportFrom) and node.module == "__future__"
                   and [item.name for item in node.names] == ["annotations"] for node in tree.body)
    for index, node in enumerate(tree.body):
        if index == 0 and isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            continue
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) and all(
            isinstance(target, ast.Name) and not target.id.startswith("__") for target in node.targets
        ):
            continue
        if isinstance(node, ast.ImportFrom) and node.level == 0 and (
            (node.module == "__future__" and [item.name for item in node.names] == ["annotations"])
            or (node.module == "sqlglot" and all(item.name == "exp" for item in node.names))
        ):
            # The package initializer imports exp before any of its modules are discovered.
            continue
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not node.decorator_list:
            args = node.args
            annotations = [a.annotation for a in args.posonlyargs + args.args + args.kwonlyargs]
            annotations += [a.annotation for a in (args.vararg, args.kwarg) if a is not None]
            if (not deferred and any(a is not None for a in [node.returns, *annotations])) or not all(
                value is None or isinstance(value, ast.Constant) for value in args.defaults + args.kw_defaults
            ):
                return False
            continue
        return False
    return True


CHANGE = re.compile(r"diff --git a/(sqlglot/(?:[a-zA-Z0-9_]+/)*[a-zA-Z0-9_]+\.py) "
                    r"b/(sqlglot/(?:[a-zA-Z0-9_]+/)*[a-zA-Z0-9_]+\.py)")


def filesystem_change(text, base):
    """Verify simple additions, deletions, and pure same-package renames."""
    lines = text.splitlines()
    if len(lines) < 2 or len(lines) > 25000:
        return None
    paths = CHANGE.fullmatch(lines[0])
    if paths is None:
        return None
    old, new = paths.groups()
    if any(Path(path).name == "__init__.py" for path in (old, new)):
        return None
    if old != new:
        if (old.rpartition("/")[0] != new.rpartition("/")[0] or
                lines[1:] != ["similarity index 100%", "rename from " + old, "rename to " + new]):
            return None
        return old, new, (base / old).read_text()
    if lines[1] not in ("new file mode 100644", "deleted file mode 100644"):
        return None
    rest = lines[2:]
    if rest and rest[0].startswith("index "):
        rest = rest[1:]
    addition = lines[1].startswith("new")
    expected = ["--- /dev/null", "+++ b/" + new] if addition else ["--- a/" + old, "+++ /dev/null"]
    if rest[:2] != expected:
        return None
    # Reuse the standard same-path unified hunk validator after checking modes.
    normalized = "\n".join([lines[0], "--- a/" + old, "+++ b/" + new, *rest[2:]]) + "\n"
    before = "" if addition else (base / old).read_text()
    source = patched(before, split_diff(normalized)[old])
    if addition:
        compile(source, new, "exec", dont_inherit=True)
        return None, new, source
    return (old, None, before) if source == "\n" else None
