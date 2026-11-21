"""Build match, coverage and size features without retaining patch or test identities."""
import ast
from difflib import SequenceMatcher
import keyword
import math
from pathlib import Path
import re

from impact import UnknownChange, patched, split_diff, syntax
from module_topology import addition, hard_topology, opaque_format
from observation_index import default_changes
from receiver_edits import classes, declaration
from refine import changed_fields
from registry_guard import changes_routing

FORMS = ("body", "member", "scalar declaration", "fixture", "mixed", "layout")
WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]+")
FEATURE_COUNT = len(FORMS) + 19


def member_change(before, after):
    """Recognize one plain added or removed member, including typed methods."""
    prior = dict(classes(before.body))
    current = dict(classes(after.body))
    reference = ast.dump(before)
    for owner, old in prior.items():
        new = current.get(owner)
        if new is None or abs(len(new.body) - len(old.body)) != 1:
            continue
        before_text = list(map(ast.dump, old.body))
        after_text = list(map(ast.dump, new.body))
        changes = [v for v in SequenceMatcher(None, before_text, after_text, autojunk=False).get_opcodes()
                   if v[0] != "equal"]
        if len(changes) != 1:
            continue
        tag, i, j, k, stop = changes[0]
        if tag not in ("insert", "delete"):
            continue
        item = new.body[k] if tag == "insert" else old.body[i]
        if not isinstance(item, ast.FunctionDef) or item.decorator_list or item.name.startswith("__"):
            continue
        saved = new.body
        try:
            new.body = saved[:k] + old.body[i:j] + saved[stop:]
            if ast.dump(after) == reference:
                return owner, item.name
        finally:
            new.body = saved
    return None


def slot_declarations(tree):
    def assignments():
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                yield node, node.targets
            elif isinstance(node, ast.AnnAssign):
                yield node, (node.target,)

    return tuple(ast.dump(node) for node, targets in assignments()
                 if any(isinstance(target, ast.Name) and target.id == "__slots__" for target in targets))


def symbol_words(source):
    return {word.casefold().strip("_") for word in WORD.findall(source)
            if len(word.strip("_")) >= 3}


class DropDocstrings(ast.NodeTransformer):
    def generic_visit(self, node):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            if ast.get_docstring(node, clean=False) is not None:
                node.body = node.body[1:]
        return super().generic_visit(node)


def layout_only(a, b):
    left, right = (ast.dump(DropDocstrings().visit(ast.parse(text))) for text in (a, b))
    return left == right


def edit_lines(hunks):
    lines = set()
    added = removed = 0
    for start, _, a, b in hunks:
        for tag, i, j, k, length in SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
            if tag == "equal":
                continue
            if i != j:
                lines.update(range(start + i + 1, start + j + 1))
            else:
                lines.update((max(1, start + i), start + i + 1))
            removed += j - i
            added += length - k
    return lines, added, removed


def capped_count(bit, masks):
    return min(15, sum(bool(mask & bit) for mask in masks))


class RiskFeatures:
    def __init__(self, impact, names, precision, scheduler, tests):
        self.impact = impact
        self.names = names
        self.precision = precision
        self.scheduler = scheduler
        counts = {}
        for row in tests:
            counts[row["file"]] = counts.get(row["file"], 0) + 1
        self.tests = {row["number"]: (Path(row["file"]).stem.casefold().removeprefix("test_"),
                                      counts[row["file"]]) for row in tests}

    def analyze(self, text, guard_bindings=False):
        if len(text) > 24000 or hard_topology(text) or opaque_format(text) or addition(text):
            return None
        try:
            edits = split_diff(text)
            if not edits or len(edits) > 4:
                return None
            composite = len(edits) > 1
            families = set()
            lines = []
            functions = []
            words = set()
            modules = set()
            py = sql = added = removed = 0
            for path, hunks in edits.items():
                affected, a, r = edit_lines(hunks)
                if len(affected) > 128:
                    return None
                added += a
                removed += r
                stem = Path(path).stem.casefold()
                modules.add(stem)
                if path.startswith("tests/fixtures/") and path.endswith(".sql"):
                    if not (self.impact.root / path).is_file():
                        return None
                    families.add("fixture")
                    sql += 1
                    continue
                if (not path.startswith("sqlglot/") or not path.endswith(".py")
                        or path.endswith("/__init__.py")):
                    return None
                py += 1
                source, (outer, before), _ = self.impact.base(path)
                new = patched(source, hunks)
                if source == new:
                    return None
                after_ast, (new_outer, after), _ = syntax(path, new)
                if outer == new_outer and before.keys() == after.keys():
                    sites = [key for key in before if before[key][2] != after[key][2]]
                    families.add("body" if sites else "layout")
                else:
                    sites = [key for key in before if key not in after or before[key][2] != after[key][2]]
                    old_ast = syntax(path, source)[0]
                    member = declaration(old_ast, after_ast) or member_change(old_ast, after_ast)
                    if guard_bindings and composite and ("__slots__" in source or "__slots__" in new):
                        if slot_declarations(old_ast) != slot_declarations(after_ast):
                            return None
                    if member:
                        families.add("member")
                        words.update(part.casefold() for part in (*member[0].split("."), member[1]))
                    elif default_changes(old_ast, after_ast) or changed_fields(source, new):
                        families.add("scalar declaration")
                    elif layout_only(source, new):
                        families.add("layout")
                    else:
                        return None
                for key in sites:
                    qual = ".".join(key[:-1])
                    words.update(part.casefold().strip("_") for part in key[:-1] if len(part.strip("_")) > 2)
                    first = before[key][0]
                    functions.append(self.precision.mask(f"F:{path}:{first}:{qual}"))
                for number in affected:
                    lines.append(self.precision.mask(f"L:{path}:{number}"))
                for key in after.keys() - before.keys():
                    words.update(part.casefold().strip("_") for part in key[:-1] if len(part.strip("_")) > 2)
            if py and changes_routing(self.impact, text):
                return None
            form = "mixed" if len(edits) > 1 else next(iter(families))
            changed_text = "\n".join(line[1:] for line in text.splitlines()
                                     if line.startswith(("+", "-")) and not line.startswith(("+++", "---")))
            words.update(word for word in symbol_words(changed_text)
                         if not keyword.iskeyword(word) and word not in {"self", "cls"})
            lexical = [self.names.mask("word:" + word) for word in sorted(words)[:40]]
            dialects = [self.names.mask("word:" + stem) for stem in sorted(modules)]
            common = tuple(int(form == other) for other in FORMS) + (
                len(edits), py, sql, min(128, added), min(128, removed), len(functions),
                len(lines), len(words),
            )
            return FORMS.index(form), common, lines, functions, lexical, dialects, modules
        except (OSError, UnknownChange, SyntaxError, UnicodeError, RecursionError, ValueError):
            return None

    def rows(self, text, selected, rules=None, guard_bindings=False):
        context = self.analyze(text, guard_bindings)
        if context is None:
            return None
        form, common, lines, functions, lexical, dialects, modules = context
        count = len(selected)
        rules = rules or {}
        routing = (int(count == len(self.tests)), int(bool(rules.get("receiver_declaration"))),
                   int(bool(rules.get("line_cleared"))))
        cohort = int(math.log2(max(1, count)))
        output = []
        for n in selected:
            bit = self.scheduler.bits[n]
            stem, test_count = self.tests[n]
            output.append(common + routing + (
                cohort, min(30000, self.scheduler.durations[n]), min(test_count, 200),
                capped_count(bit, lines), capped_count(bit, functions),
                capped_count(bit, lexical), int(any(bit & mask for mask in dialects)),
                int(stem in modules),
            ))
        return form, output
