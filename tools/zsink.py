"""Name-lookup map of the pinned source, for edits that add or delete a method.

A method edit changes what an attribute lookup by that name returns. Static lookups are
attribute instructions in code objects; dynamic ones go through getattr-like calls and
member enumeration. For every Python file in sqlglot/ and tests/, this records:

- attrs: for each attribute name, the code objects whose own instructions load, store or
  delete it and the lines of those instructions (keys as in zmap.xz: path, qualname, first
  line);
- constants: every identifier word in a string constant passed to a name-lookup call;
- dynamic: code objects with a name-lookup call whose name is not a constant, or that
  enumerate members, with the site's line and the name suffix the site is limited to (""
  when any name; "<module>" for audited sites that only look up module attributes).

usage: zsink.py ROOT OUT [--audit]
"""

import ast
import dis
import lzma
import marshal
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from zpatch import lookup_sites as site_kinds  # noqa: E402

ROOT, OUT = map(os.path.abspath, sys.argv[1:3])
AUDIT = "--audit" in sys.argv
ATTR_OPS = {"LOAD_ATTR", "STORE_ATTR", "DELETE_ATTR", "LOAD_SUPER_ATTR", "LOAD_METHOD"}
# Audited pinned sites whose object is always a module (path, line): plugin and lazy loaders,
# class listing by module and deserialization by class name.
MODULE_ONLY = {("sqlglot/helper.py", 153), ("sqlglot/serde.py", 120), ("sqlglot/dialects/__init__.py", 125),
               ("sqlglot/dialects/dialect.py", 249), ("sqlglot/dialects/dialect.py", 250),
               ("sqlglot/optimizer/__init__.py", 57)}
# Audited dir() sites that keep only public names ending in "_sql".
SQL_ONLY = {("sqlglot/generator.py", 79), ("sqlglot/generator.py", 87), ("sqlglot/optimizer/simplify.py", 1982),
            ("sqlglot/optimizer/simplify.py", 1984)}


def code_objects(code):
    yield code
    for const in code.co_consts:
        if isinstance(const, type(code)):
            yield from code_objects(const)


attrs, constants, dynamic = {}, set(), set()
for top in ("sqlglot", "tests"):
    for directory, _, filenames in sorted(os.walk(os.path.join(ROOT, top))):
        for filename in sorted(filenames):
            if not filename.endswith(".py"):
                continue
            full = os.path.join(directory, filename)
            path = full[len(ROOT) + 1:]
            with open(full, encoding="utf-8", newline="") as f:
                text = f.read()
            try:
                tree = ast.parse(text)
                module = compile(text, full, "exec", dont_inherit=True)
            except SyntaxError:
                constants.add(None)  # unknown file: the stage keeps the old rule
                continue
            owner = {}
            for code in code_objects(module):
                key = (path, code.co_qualname, code.co_firstlineno)
                for _, _, line in code.co_lines():
                    if line is not None:
                        owner.setdefault(line, set()).add(key)
                line = code.co_firstlineno
                for ins in dis.get_instructions(code):
                    if ins.line_number is not None:
                        line = ins.line_number
                    if ins.opname in ATTR_OPS:
                        name = ins.argval if isinstance(ins.argval, str) else ins.argrepr.split()[0]
                        attrs.setdefault(name, set()).add(key + (line,))
            for node, words, suffix in site_kinds(tree):
                if words is not None:
                    constants.update(words)
                    continue
                # The innermost code object that owns the call's line.
                found = [k for k in owner.get(node.lineno, ()) if k[2] <= node.lineno]
                # The innermost code object on that line: the latest first line.
                found = sorted(k for k in found if k[2] == max(k[2] for k in found))
                if not found:
                    constants.add(None)
                    continue
                if (path, node.lineno) in MODULE_ONLY:
                    suffix = "<module>"
                elif (path, node.lineno) in SQL_ONLY:
                    suffix = "_sql"
                dynamic.add((path, node.lineno, suffix, tuple(found)))
                if AUDIT:
                    print(f"{path}:{node.lineno} {[k[1] for k in found]} suffix={suffix!r}: "
                          f"{ast.unparse(node)[:110]}", file=sys.stderr)

data = {"attrs": {n: sorted(k) for n, k in attrs.items()}, "constants": sorted(c for c in constants if c),
        "unknown": None in constants,
        "dynamic": sorted(dynamic)}
with open(OUT, "wb") as f:
    f.write(lzma.compress(marshal.dumps(data), preset=9 | lzma.PRESET_EXTREME))
print(len(attrs), "names", len(data["constants"]), "constants", len(dynamic), "dynamic sites",
      "unknown" if data["unknown"] else "", file=sys.stderr)
