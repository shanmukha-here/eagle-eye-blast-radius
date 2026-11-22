"""Exact candidates for edits that only swap function code on the pinned import.

A test that never started a changed code object on the pin runs the same instructions on the
patched source, so it passes as it did on the pin. The map lists, for each code object, the
tests that started it in a fork of the fully imported suite. Code that already ran while
importing is excluded, because its effects persist without re-running it.
"""

import lzma
import marshal
import os
import time
import types
from pathlib import Path

import zpatch
from impact import UnknownChange, patched, split_diff


class Candidates:
    def __init__(self, map_path, base, ids):
        self.base = Path(base)
        self.ready = False
        try:
            data = marshal.loads(lzma.decompress(Path(map_path).read_bytes()))
        except (OSError, ValueError, EOFError, lzma.LZMAError):
            return
        if data["ids"] != list(ids):
            return
        self.ids = data["ids"]
        self.base_map = map_path
        self.masks = dict(zip(map(tuple, data["keys"]), data["masks"]))
        self.preload = set(map(tuple, data["preload"]))
        # Code that runs differently after earlier tests changed shared state.
        self.sensitive = set(map(tuple, data.get("sensitive", ())))
        self.preload.update(self.sensitive)
        self.opens = data["opens"]
        # Tests whose own trace is unusable stay in the incoming selection.
        self.opaque = data["failed"] | data["missing"]
        self.spawn = data["spawn"]
        self.blocked = self.opaque | data["spawn"]
        for mask in data["opens"].values():
            self.blocked |= mask
        self.cpu = data.get("grouped_cpu") or data["cpu"]
        self.lines = {}
        try:
            self.lines = marshal.loads(lzma.decompress(Path(str(map_path) + ".lines").read_bytes()))
        except (OSError, ValueError, EOFError, lzma.LZMAError):
            self.lines = {}
        self.classes = None
        try:
            self.classes = marshal.loads(lzma.decompress(Path(map_path).with_name("zclass.xz").read_bytes()))
        except (OSError, ValueError, EOFError, lzma.LZMAError):
            self.classes = None
        self.sinks = None
        try:
            sinks = marshal.loads(lzma.decompress(Path(map_path).with_name("zsink.xz").read_bytes()))
            if not sinks["unknown"]:
                self.sinks = {"attrs": sinks["attrs"], "constants": set(sinks["constants"]),
                              "dynamic": [(f, line, suffix, [tuple(k) for k in owners])
                                          for f, line, suffix, owners in sinks["dynamic"]]}
        except (OSError, ValueError, EOFError, KeyError, TypeError, lzma.LZMAError):
            self.sinks = None
        self.fixtures = None
        try:
            self.fixtures = marshal.loads(lzma.decompress(Path(map_path).with_name("zfix.xz").read_bytes()))
            self.fixtures["collect"] = set(self.fixtures["collect"])
        except (OSError, ValueError, EOFError, KeyError, TypeError, lzma.LZMAError):
            self.fixtures = None
        self.values = None
        try:
            attr = marshal.loads(lzma.decompress(Path(map_path).with_name("zattr.xz").read_bytes()))
            if attr["ids"] == self.ids:
                self.values = {
                    "map": str(Path(map_path).resolve().with_name("zval.xz")),
                    "reads": dict(zip(map(tuple, attr["keys"]), attr["masks"])),
                    "collect": set(map(tuple, attr["collect"])),
                    "tracked": set(map(tuple, attr["tracked"])),
                    "failed": attr["failed"],
                    "globals": {tuple(k): [(tuple(c), f) for c, f in v] for k, v in attr["globals"].items()},
                    "codes": None,
                    "import_only": (set(map(tuple, attr.get("import_codes", ())))
                                    - set(map(tuple, attr.get("collect_codes", ())))
                                    - self.sensitive),
                }
        except (OSError, ValueError, EOFError, KeyError, TypeError, lzma.LZMAError):
            self.values = None
        self.inverted = None
        self.by_name = {}
        for key in list(self.masks) + list(self.preload):
            self.by_name.setdefault((key[0], key[1]), []).append(key)
        self.ready = True
        self.stats = {}

    def source(self, path):
        with open(self.base / path, encoding="utf-8", newline="") as f:
            return f.read()

    def _refs(self, name, kind, patched):
        """Files other than ``patched`` whose pinned source has ``name`` as a ``kind``
        (0 identifier, 1 string, 2 compared, 3 non-call reference), plus patched files whose
        new source does."""
        if self.inverted is None:
            self.inverted = inverted = [{}, {}, {}, {}]
            for path, sets in self.classes["files"].items():
                if sets is None:
                    for table in inverted:
                        table.setdefault(None, set()).add(path)
                    continue
                for table, values in zip(inverted, sets):
                    for value in values:
                        table.setdefault(value, set()).add(path)
        found = set(self.inverted[kind].get(name, ())) | set(self.inverted[kind].get(None, ()))
        found -= set(patched)
        for path, sets in patched.items():
            if sets is None or name in sets[kind]:
                found.add(path)
        return found

    def _resolve(self, path, scope, name):
        """Code entries that instances and subclasses of the class resolved ``name`` to on the
        pin, below the class itself, and whether some subclass had no such attribute; None
        when unknown."""
        data = self.classes
        number = data["unique"].get((path, scope))
        if number is None or number < 0:
            return None
        entries, absent = set(), False
        for record_path, mro, own in data["classes"]:
            if number not in mro:
                continue
            at = mro.index(number)
            if any(not isinstance(m, str) and name in data["classes"][m][2] for m in mro[:at]):
                continue
            for m in mro[at + 1:]:
                if isinstance(m, str):
                    if name in data["external"].get(m, ()) or m not in data["external"]:
                        return None
                    continue
                table = data["classes"][m][2]
                if name in table:
                    if table[name] is None:
                        return None
                    entries.add((table[name][0], tuple(table[name][1])))
                    break
            else:
                absent = True
        return entries, absent

    def _members(self, path, result, patched, results=None, texts=None):
        """(keys, mask) whose tests cover every test an added or deleted method can affect,
        or None."""
        if self.classes is None:
            return None
        keys, extra = [], 0
        for scope, name, passthrough in result["added"]:
            if name.startswith("__"):
                return None
            named = self._refs(name, 1, patched) or self._refs(name, 2, patched)
            if not scope:
                # A new module function is unseen unless something names it.
                if named or self._refs(name, 0, patched):
                    return None
                continue
            found = self._resolve(path, scope, name)
            if found is None:
                return None
            entries, absent = found
            if absent and name.endswith("_sql") and not name.startswith("_"):
                return None
            if named or absent and self._refs(name, 0, patched):
                # The name is used elsewhere: cover the tests that look it up.
                reach = self._reach(path, scope, name, results, texts)
                if reach is None:
                    return None
                keys.extend(reach[0])
                extra |= reach[1]
            if passthrough and not absent and all(kind == "function" for kind, _ in entries):
                # Calls reach the same inherited function with the same arguments.
                continue
            keys.extend(key for _, key in entries)
        for scope, name, key in result["removed"]:
            if (name.startswith("__") and not (scope and name in PROTOCOL_DUNDERS)) \
                    or self._refs(name, 1, patched) or self._refs(name, 2, patched) \
                    or self._refs(name, 3, patched):
                reach = None
                if scope and not name.startswith("__"):
                    reach = self._reach(path, scope, name, results, texts)
                if reach is None:
                    return None
                keys.extend(reach[0])
                extra |= reach[1]
            keys.append(tuple(key))
        return keys, extra

    def _reach(self, path, scope, name, results, texts):
        """(code keys, mask) of the tests whose attribute lookups of ``name`` can see a method
        of that name added to or deleted from class ``scope``, or None when unproven.

        A lookup by a static name is an attribute instruction, and it only acts in a test
        that ran its line. A lookup by data is a getattr-like call or a member listing: one
        with a constant name declines here, and the others act where they ran. Lookups in
        code that ran before the tests are returned as keys, which the caller declines or
        sends to the value diff like any other import-time code."""
        s = self.sinks
        if s is None or results is None or name in s["constants"] or name in PROTOCOL_NAMES \
                or name.startswith(PROTOCOL_PREFIXES) or (name.startswith("_") and name.endswith("_")):
            return None
        # Python and the standard library call other names on objects of classes with
        # foreign bases (enums, exceptions, ABCs, containers).
        data = self.classes
        number = data["unique"].get((path, scope))
        if number is None or number < 0:
            return None
        for _, mro, _ in data["classes"]:
            if number in mro and any(isinstance(m, str) and m != "builtins.object" for m in mro):
                return None
        if self._new_lookups(name, results, texts):
            return None
        keys, mask = [], 0
        sites = [((f, q, first), f, line) for f, q, first, line in s["attrs"].get(name, ())]
        for f, line, suffix, owners in s["dynamic"]:
            if suffix == "<module>" or suffix and not name.endswith(suffix):
                continue
            sites.extend((key, f, line) for key in owners)
        for key, f, line in sites:
            if key in self.preload:
                keys.append(key)
                continue
            table = self.lines.get(f)
            if table is not None and line in table:
                mask |= table[line]
            else:
                mask |= self.masks.get(key, 0)
        return keys, mask

    def _new_lookups(self, name, results, texts):
        """Whether a patched file's new text looks ``name`` up somewhere the pin did not."""
        import ast
        import dis
        attrs = self.sinks["attrs"].get(name, ())
        for other, new in (texts or {}).items():
            try:
                old_tree = ast.parse(results[other]["old_text"])
                new_tree = ast.parse(new)
                code = compile(new, other, "exec", dont_inherit=True)
            except (SyntaxError, ValueError, KeyError, RecursionError, MemoryError):
                return True
            old_sites = {ast.dump(n) for n, words, _ in zpatch.lookup_sites(old_tree) if words is None}
            for node, words, _ in zpatch.lookup_sites(new_tree):
                if (name in words) if words is not None else ast.dump(node) not in old_sites:
                    return True
            pinned = {q for f, q, _, _ in attrs if f == other}
            changed = {k[1] for k in results[other]["changed"]}
            stack = [code]
            while stack:
                c = stack.pop()
                stack.extend(k for k in c.co_consts if isinstance(k, types.CodeType))
                if name not in c.co_names:
                    continue
                if not any(ins.opname in ATTR_OPS and ins.argval == name for ins in dis.get_instructions(c)):
                    continue
                q = c.co_qualname
                if q in pinned or any(q == p or q.startswith(p + ".") for p in changed):
                    continue
                return True
        return False

    def _input_only(self, path):
        """Tests that read only the inputs (meta and SQL) of this fixture's pairs."""
        prefix, keep = INPUT_ONLY_READERS[path]
        return sum(1 << i for i, t in enumerate(self.ids) if t.startswith(prefix) and t not in keep)

    def _value_mask(self, files, members=()):
        """(mask, code keys) covering every test that can see a module or class value change
        after importing ``files`` ({path: new text}) in place of the pin, or None."""
        import subprocess
        import sys
        v = self.values
        if v is None:
            self.stats["values_decline"] = "map"
            return None
        if v["codes"] is None:
            try:
                data = marshal.loads(lzma.decompress(Path(v["map"]).read_bytes()))
            except (OSError, ValueError, EOFError, lzma.LZMAError):
                self.stats["values_decline"] = "map"
                return None
            if data.get("python") != __import__("importlib.util").util.MAGIC_NUMBER:
                self.stats["values_decline"] = "python"
                return None
            v["codes"] = {tuple(k): [tuple(c) for c in cs] for k, cs in data["codes"].items()}
        here = os.path.dirname(os.path.abspath(__file__))
        env = dict(os.environ, PYTHONHASHSEED="0", PYTHONDONTWRITEBYTECODE="1")
        t0 = time.monotonic()
        try:
            out = subprocess.run([sys.executable, "-B", os.path.join(here, "zvalues.py")],
                                 input=marshal.dumps((str(self.base.resolve()), v["map"], files)), env=env,
                                 cwd=str(self.base.resolve()), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                 timeout=30).stdout
            result = marshal.loads(out)
        except (OSError, ValueError, EOFError, subprocess.SubprocessError):
            self.stats["values_decline"] = "child"
            return None
        self.stats["values_wall"] = round(time.monotonic() - t0, 3)
        if not isinstance(result, tuple) or len(result) != 3 or result[0] == "error":
            self.stats["values_decline"] = "error"
            return None
        changed, added, removed = result
        self.stats["values_changed"] = len(changed)
        # The patch's own added and deleted functions are bounded by the member rules.
        added = [k for k in map(tuple, added) if k not in members]
        removed = [k for k in map(tuple, removed) if k not in members]
        if added or removed:
            self.stats["values_decline"] = "added"
            return None
        mask, keys = v["failed"], []
        for key in map(tuple, changed):
            if key in v["collect"]:
                self.stats["values_decline"] = "collect"
                return None
            codes = v["codes"].get(key)
            if codes is not None:
                # A function-like value acts only when its code runs.
                keys.extend(codes)
            if key[0] == "c":
                if key in v["tracked"]:
                    mask |= v["reads"].get(key, 0)
                elif codes is None:
                    self.stats["values_decline"] = "untracked"
                    return None
                continue
            mask |= v["reads"].get(key, 0) | v["reads"].get(("m", key[1], "__dict__"), 0)
            for code, is_function in v["globals"].get((key[1], key[2]), ()):
                if code in self.preload:
                    if is_function:
                        self.stats["values_decline"] = "preload_fn"
                        return None
                    continue
                mask |= self.masks.get(code, 0)
        return mask, keys

    def analyze(self, text, gain=None):
        """Return (mask of tests that may change, analyses) or None when unsupported."""
        self.stats = {}
        if not self.ready or not text.endswith("\n"):
            self.stats.setdefault("decline", "3:if not self.ready or not text.endswith('")
            return None
        try:
            files = split_diff(text, physical=True)
        except UnknownChange:
            self.stats.setdefault("decline", "7:except UnknownChange:")
            return None
        mask = self.opaque
        results = {}
        changed = set()
        executable = True
        member_files = {}
        ranges_out = {}
        # Tests that fixture or member edits may reach; the zygote applies neither.
        unswapped = 0
        texts = {}
        for path, hunks in files.items():
            if path.startswith("tests/fixtures/") and not path.endswith(".py"):
                # A data file reaches only the tests that open it while they run.
                if self.fixtures is None or path in self.fixtures["collect"]:
                    self.stats.setdefault("decline", "21:if self.fixtures is None or path in self")
                    return None
                try:
                    new_fixture = patched(self.source(path), hunks, physical=True)
                except (OSError, UnicodeError, UnknownChange, ValueError):
                    self.stats.setdefault("decline", "25:except (OSError, UnicodeError, UnknownCh")
                    return None
                opens = self.fixtures["opens"].get(path, 0)
                if path in INPUT_ONLY_READERS:
                    from narrow import fixture_inputs
                    if fixture_inputs(self.source(path)) == fixture_inputs(new_fixture):
                        opens &= ~self._input_only(path)
                mask |= opens
                unswapped |= opens
                executable = False
                continue
            if not (path.startswith("sqlglot/") and path.endswith(".py")):
                self.stats.setdefault("decline", "31:if not (path.startswith('sqlglot/') and ")
                return None
            try:
                old = self.source(path)
                new = patched(old, hunks, physical=True)
                result = zpatch.analyze(path, old, new, str(self.base / path),
                                        values=self.values is not None)
            except (OSError, UnicodeError, UnknownChange, zpatch.Unsupported, ValueError,
                    RecursionError, MemoryError, TypeError):
                self.stats.setdefault("decline", "39:RecursionError, MemoryError, TypeError):")
                return None
            if result["added"] or result["removed"]:
                member_files[path] = result["names"]
            if result["noswap"]:
                # The zygote cannot swap code whose closure or nested code changed shape; it
                # replays only tests that never started that code.
                executable = False
                for key in result["noswap"]:
                    unswapped |= self.masks.get(key, 0)
            results[path] = result
            texts[path] = new
        need_values = any(r["values"] for r in results.values())
        member_keys = set()
        for path, result in results.items():
            hunks = files[path]
            old = result["old_text"]
            line_keys = {}
            file_lines = self.lines.get(path)
            if file_lines is not None and result["changed"]:
                try:
                    tree = __import__("ast").parse(old)
                    nodes = zpatch.function_nodes(tree)
                    edited, inserts = zpatch.changed_lines(hunks)
                    for key in result["scoped"]:
                        ranges = zpatch.line_ranges(tree, nodes, key, edited, inserts)
                        if ranges is not None:
                            m = 0
                            for a, b in ranges:
                                for line in range(a, b + 1):
                                    m |= file_lines.get(line, 0)
                            line_keys[key] = m
                            ranges_out[key] = [(path, a, b) for a, b in ranges]
                except (SyntaxError, ValueError, RecursionError, MemoryError):
                    line_keys = {}
            keys = list(result["changed"])
            if result["added"] or result["removed"]:
                # The zygote cannot add or delete methods, so these commits are not executed.
                executable = False
                for scope, name, _ in result["added"]:
                    member_keys.add(("c", path, scope, name) if scope else ("m", path, name))
                for scope, name, _ in result["removed"]:
                    member_keys.add(("c", path, scope, name) if scope else ("m", path, name))
                    if name.startswith("__"):
                        need_values = True
                more = self._members(path, result, member_files, results, texts)
                if more is None:
                    self.stats.setdefault("decline", "72:if more is None:")
                    return None
                more, extra = more
                keys.extend(more)
                mask |= extra
                unswapped |= extra
                for key in more:
                    unswapped |= self.masks.get(key, 0)
            for name, _, _ in result["defaults"]:
                found = self.by_name.get((path, name))
                if not found:
                    # A default can only matter if the function runs. A function of the pinned
                    # file that no test started adds nothing; an unknown name declines.
                    if name not in _qualnames(old, str(self.base / path)):
                        self.stats.setdefault("decline", "80:# A default can only matter if the funct")
                        return None
                    continue
                keys.extend(found)
            for key in keys:
                if key in self.preload:
                    # Code that ran only while importing sqlglot left its effects in values
                    # that the value diff compares.
                    if self.values is None or key not in self.values["import_only"]:
                        self.stats.setdefault("decline", "87:if self.values is None or key not in sel")
                        return None
                    need_values = True
                if key in line_keys:
                    mask |= line_keys[key] & self.masks.get(key, 0)
                else:
                    mask |= self.masks.get(key, 0)
                changed.add(key)
            mask |= self.opens.get(path, 0)
        if need_values and gain is not None and gain(mask) < VALUE_MIN_GAIN:
            # The import child costs about a CPU second; even a value change no test reads
            # would not save that much here.
            self.stats.setdefault("decline", "value_gain")
            return None
        if need_values:
            found = self._value_mask(texts, member_keys)
            if found is None:
                self.stats.setdefault("decline", "98:if found is None:")
                return None
            # The zygote does not apply value edits, so these commits are not executed.
            executable = False
            mask |= found[0]
            unswapped |= found[0]
            value_keys = found[1]
            for key in value_keys:
                unswapped |= self.masks.get(key, 0)
        else:
            value_keys = []
        for key in value_keys:
            if key in self.preload and key not in self.values["import_only"]:
                self.stats.setdefault("decline", "110:if key in self.preload and key not in se")
                return None
            mask |= self.masks.get(key, 0)
            changed.add(key)
        return mask, results, changed, executable, ranges_out, unswapped


def _qualnames(text, filename):
    """Qualified names of the functions compiled from a source."""
    out, stack = set(), [compile(text, filename, "exec", dont_inherit=True)]
    while stack:
        code = stack.pop()
        if code.co_flags & 0x2:
            out.add(code.co_qualname)
        stack.extend(c for c in code.co_consts if isinstance(c, types.CodeType))
    return out


def swap_text(text):
    """The sections of a unified diff that edit sqlglot/*.py files."""
    parts = text.split("\ndiff --git ")
    keep = []
    for k, part in enumerate(parts):
        section = part if k == 0 else "diff --git " + part
        header = section.split("\n", 1)[0]
        bits = header.split(" ")
        if len(bits) == 4 and bits[2].startswith("a/sqlglot/") and bits[2].endswith(".py"):
            keep.append(section)
    out = "\n".join(keep)
    return out if out.endswith("\n") or not out else out + "\n"


VALUE_MIN_GAIN = 4.0
# TestExecutor loads both TPC fixtures in setUpClass (so every test in the class opens them),
# but only test_optimized_tpch reads the expected side of a pair (tests/test_executor.py on the
# pin). When an edit leaves every pair's input the same, the other tests read the same data.
INPUT_ONLY_READERS = {
    "tests/fixtures/optimizer/tpc-ds/tpc-ds.sql": ("tests/test_executor.py::TestExecutor::", ()),
    "tests/fixtures/optimizer/tpc-h/tpc-h.sql": (
        "tests/test_executor.py::TestExecutor::",
        ("tests/test_executor.py::TestExecutor::test_optimized_tpch",)),
}
# Dunders that Python only looks up to call. Deleting one changes only the calls that started
# it; the value diff checks the class dict for side effects such as an implicit __hash__.
PROTOCOL_DUNDERS = {
    "__repr__", "__str__", "__format__", "__hash__", "__eq__", "__ne__", "__lt__", "__le__",
    "__gt__", "__ge__", "__bool__", "__len__", "__iter__", "__next__", "__reversed__",
    "__contains__", "__getitem__", "__setitem__", "__delitem__", "__call__", "__copy__",
    "__deepcopy__", "__add__", "__radd__", "__sub__", "__rsub__", "__mul__", "__rmul__",
    "__truediv__", "__and__", "__or__", "__xor__", "__invert__", "__neg__", "__pos__",
    "__enter__", "__exit__",
}
# Names that Python, the standard library or the test runner look up on objects passed to
# them (mappings, files, generators, test cases), and prefixes of dispatch by name.
PROTOCOL_NAMES = {
    "keys", "read", "readline", "readlines", "readinto", "peek", "write", "writelines", "flush",
    "close", "fileno", "seek", "tell", "isatty", "send", "throw", "asend", "athrow", "aclose",
    "mro", "setUp", "tearDown", "setUpClass", "tearDownClass", "addCleanup", "run", "debug",
    "id", "shortDescription", "countTestCases", "subTest", "default", "load_tests",
}
PROTOCOL_PREFIXES = ("test", "visit_", "do_", "handle_", "pytest_")
ATTR_OPS = {"LOAD_ATTR", "STORE_ATTR", "DELETE_ATTR", "LOAD_SUPER_ATTR"}
WALL0 = time.monotonic()
LONG_MS = 3000
# Stages that run tests in the zygote. Short-test refinement cost more CPU than the simulated
# time it saved in Docker (hidden-01: 48.9 CPU-s for about 19 s).
REPLAY_LONG = True
REFINE_SHORT = False
# Run short candidates only when all of a plan is short, stopping at the first failure: only
# an empty plan saves enough (its workers' startup) to pay for the CPU.
EMPTY_ONLY = True
# Replay TPC queries up to row execution only, when no affected query runs changed code there.
REPLAY_PLANS = True
PLAN_CPU = 0.35
# Run a fixture-driven test on only the fixture cases that started changed code (zcase.xz).
REPLAY_CASES = True
# A replay costs about the recorded seconds of the part it runs, plus the fork.
CASE_CPU = 1.0
CASE_FORK = 0.05


class Execute:
    """Run short candidates on the patched code when the possible saving pays for the CPU."""

    def __init__(self, swap, scheduler, base, ids, n_patches):
        self.swap = swap
        self.scheduler = scheduler
        limit = 180.0 if n_patches <= 40 else 270.0
        self.deadline = WALL0 + 0.45 * limit
        self.remaining = 1.5 * n_patches
        self.zygote = None
        if swap.ready:
            from zexec import Zygote
            self.zygote = Zygote(base, ids, self.deadline)
        self.cpu = swap.cpu
        self.stats = {}
        self.tpc = None
        try:
            data = marshal.loads(lzma.decompress(Path(swap.base_map).with_name("ztpc.xz").read_bytes()))
            self.tpc = data
            self.tpc_setup = set(map(tuple, data["setup"]))
            self.tpc_units = {}
            for name, (number, units) in data["tests"].items():
                if swap.ids[number] != data.get("ids", {}).get(name, swap.ids[number]):
                    continue
                coded = [(set(map(tuple, c)), d, t) for c, d, t in units]
                covered = set().union(*(c for c, _, _ in coded))
                split = data.get("split", {}).get(name)
                if split is not None:
                    split = [(set(map(tuple, r)), pre, cpu) for r, pre, cpu in split]
                self.tpc_units[number] = (name, coded, covered, data.get("lines", {}).get(name), split)
        except (OSError, ValueError, EOFError, KeyError, TypeError, lzma.LZMAError):
            self.tpc = None
        self.case_map = None
        try:
            data = marshal.loads(lzma.decompress(Path(swap.base_map).with_name("zcase.xz").read_bytes()))
            if data["ids"] == swap.ids:
                data["index"] = {tuple(k): i for i, k in enumerate(data["keys"])}
                self.case_map = data
        except (OSError, ValueError, EOFError, KeyError, TypeError, lzma.LZMAError):
            self.case_map = None

    def _affected_cases(self, number, record, changed, ranges):
        """Bit mask of the record's cases that started changed code (or ran an edited line),
        or None when some change ran outside the cases, depends on case order, or the two
        traces disagree."""
        index = self.case_map["index"]
        bad_codes = record.setdefault("bad_code_set", set(record["bad_codes"]))
        bad_lines = record.setdefault("bad_line_set", set(map(tuple, record["bad_lines"])))
        bit = 1 << number
        affected = 0
        for key in changed:
            if not self.swap.masks.get(key, 0) & bit:
                continue
            i = index.get(key)
            if ranges and key in ranges and key[0] in self.swap.lines:
                table = self.swap.lines[key[0]]
                hit = False
                for path, a, b in ranges[key]:
                    for line in range(a, b + 1):
                        if not table.get(line, 0) & bit:
                            continue
                        hit = True
                        if i is None or (i, line) in bad_lines:
                            return None
                        found = record["lines"].get((i, line))
                        if found is None:
                            return None
                        affected |= found
                if hit:
                    continue
                # No edited line ran in this test on the pin: the line rule leaves it out.
                continue
            if i is None or i in bad_codes:
                return None
            found = record["codes"].get(i)
            if found is None:
                return None
            affected |= found
        return affected

    def replay_cases(self, raw, plan, mask, changed, ranges=None, unswapped=0):
        """Drop a fixture test whose affected cases pass when run alone on the patched code."""
        if not REPLAY_CASES or self.case_map is None or self.zygote is None or not plan \
                or time.monotonic() > self.deadline - 10:
            return plan
        s = self.scheduler
        selected = {n for group in plan for n in group}
        tests = self.case_map["tests"]
        # The zygote serves patched sources to imports, not to open(); subprocesses import
        # the pin.
        skip = self.swap.opaque | self.swap.spawn | unswapped
        for line in raw.splitlines():
            if line.startswith("diff --git a/"):
                skip |= self.swap.opens.get(line[len("diff --git a/"):].split(" b/")[0], 0)
        options = []
        for n in selected:
            record = tests.get(n)
            if record is None or skip >> n & 1:
                continue
            affected = self._affected_cases(n, record, changed, ranges)
            if not affected:
                continue
            cases = record["cases"]
            files = {cases[j][0] for j in range(len(cases)) if affected >> j & 1}
            # Files without affected cases yield every item, as on the pin.
            run = sum(cpu for j, cpu in enumerate(record["cpu"])
                      if affected >> j & 1 or cases[j][0] not in files)
            total = sum(record["cpu"]) + record["outside_cpu"]
            share = (run + record["outside_cpu"]) / total if total > 0 else 1.0
            estimate = CASE_FORK + CASE_CPU * share * s.durations[n] / 1000
            wanted = {}
            for j in range(len(cases)):
                if affected >> j & 1:
                    wanted.setdefault(cases[j][0], []).append(cases[j][1])
            options.append((n, estimate, wanted))
        if not options:
            return plan
        options.sort(key=lambda o: -s.durations[o[0]])
        for n, estimate, wanted in options:
            if n not in selected or time.monotonic() > self.deadline - 5:
                continue
            rest = selected - {n}
            after = s.repack(rest, plan) if rest else []
            upside = (s.cost(plan) - s.cost(after)) / 1000
            if estimate > 0.6 * upside or estimate > self.remaining:
                continue
            wall = min(self.deadline - time.monotonic(), 2.0 + 4.0 * estimate)
            passed = self.zygote.cases(raw, n, wanted, 2.0 + 4.0 * estimate, 2.0 * estimate + 0.5, wall)
            self.remaining -= max(estimate, self.zygote.last_cpu)
            self.stats["case_runs"] = self.stats.get("case_runs", 0) + 1
            self.stats["case_cpu"] = round(self.stats.get("case_cpu", 0.0) + self.zygote.last_cpu, 3)
            if passed:
                self.stats["case_dropped"] = self.stats.get("case_dropped", 0) + 1
                selected = rest
                plan = after
        return plan

    def replay_long(self, raw, plan, mask, changed, ranges=None, unswapped=0):
        """Drop a TPC executor test when every query that starts changed code digests the same."""
        if not (REPLAY_LONG or REPLAY_PLANS) or self.tpc is None or not plan \
                or time.monotonic() > self.deadline - 10:
            return plan
        s = self.scheduler
        selected = {n for group in plan for n in group}
        for number, (name, units, covered, lines, split) in self.tpc_units.items():
            if number not in selected or changed & self.tpc_setup or unswapped >> number & 1:
                continue
            bit = 1 << number
            inside = {k for k in changed if self.swap.masks.get(k, 0) & bit}
            if inside - covered:
                self.stats[name + "_outside"] = True
                continue
            reach = 0
            for k in changed:
                if lines is not None and ranges and k in ranges:
                    # Queries that ran the edited statement on the pin.
                    for path, a, b in ranges[k]:
                        table = lines.get(path, {})
                        for line in range(a, b + 1):
                            reach |= table.get(line, 0)
                else:
                    for i, (c, _, _) in enumerate(units):
                        if k in c:
                            reach |= 1 << i
            affected = [i for i in range(len(units)) if reach >> i & 1]
            # The test is a candidate, so it ran the edit; if no query did, setup or code
            # outside the queries did, and replaying queries proves nothing.
            if not affected:
                continue
            # Row execution reads only the plan and tables. When no affected query started
            # changed code there, equal plan digests give equal tables.
            rows = not (REPLAY_PLANS and split is not None
                        and not any(k[0] == "sqlglot/executor/__init__.py" or k[0].startswith("tests/")
                                    for k in changed)
                        and all(split[i][1] is not None and not split[i][0] & changed for i in affected))
            if rows and (not REPLAY_LONG or any(units[i][1] is None for i in affected)):
                self.stats[name + "_rows"] = True
                continue
            if rows:
                # Traced query CPU includes the tracer; replays use about 0.6 of it.
                estimate = 0.1 + 0.6 * sum(units[i][2] for i in affected)
                expected = [units[i][1] for i in affected]
            else:
                estimate = 0.1 + PLAN_CPU * sum(split[i][2] for i in affected)
                expected = [split[i][1] for i in affected]
            rest = selected - {number}
            after = s.repack(rest, plan) if rest else []
            upside = (s.cost(plan) - s.cost(after)) / 1000
            self.stats[name + "_queries"] = len(affected)
            self.stats[name + "_estimate"] = round(estimate, 3)
            if estimate > 0.7 * upside or estimate > self.remaining:
                continue
            wall = min(self.deadline - time.monotonic(), 10.0 + 3.0 * estimate)
            digests = self.zygote.tpc(raw, [(name, i) for i in affected], 2.0 * estimate + 1.0, wall,
                                      expected, rows=rows)
            self.remaining -= max(estimate, self.zygote.last_cpu)
            self.stats[name + "_cpu"] = round(self.zygote.last_cpu, 3)
            if digests is None or len(digests) != len(affected):
                self.stats[name + "_replay"] = "incomplete"
                continue
            same = digests == expected
            self.stats[name + "_replay"] = "same" if same else "changed"
            if same:
                selected = rest
                plan = after
        return plan

    def refine(self, raw, plan, mask):
        self.stats = {}
        if not REFINE_SHORT or self.zygote is None or not plan or time.monotonic() > self.deadline - 5:
            return plan
        s = self.scheduler
        selected = {n for group in plan for n in group}
        short = {n for n in selected if s.durations[n] < LONG_MS and mask >> n & 1
                 and not self.swap.blocked >> n & 1}
        if not short or (EMPTY_ONLY and short != selected):
            return plan
        after = s.repack(selected - short, plan) if selected - short else []
        upside = (s.cost(plan) - s.cost(after)) / 1000
        # The grouped profile overstates in-process CPU about threefold.
        estimate = 0.03 + 0.35 * sum(self.cpu[n] for n in short)
        self.stats = {"swap_exec_candidates": len(short), "swap_exec_estimate": round(estimate, 3),
                      "swap_exec_upside": round(upside, 3)}
        if estimate > 0.6 * upside or estimate > self.remaining:
            return plan
        # Cheapest first: one failure keeps the plan, so find it early.
        tests = sorted(short, key=lambda n: (self.cpu[n], n))
        limits = {n: 2.0 + 4.0 * self.cpu[n] for n in tests}
        wall = min(self.deadline - time.monotonic(), 2.0 + 4.0 * estimate)
        result = self.zygote.run(raw, tests, limits, 2.0 * estimate + 0.5, wall,
                                 first_failure=EMPTY_ONLY)
        self.remaining -= max(self.zygote.last_cpu, estimate)
        self.stats["swap_exec_cpu"] = round(self.zygote.last_cpu, 3)
        if result is None:
            self.stats["swap_exec_failed"] = True
            return plan
        failed, done = result
        passed = done - failed
        self.stats.update(swap_exec_passed=len(passed), swap_exec_failing=len(failed))
        if not passed or (EMPTY_ONLY and passed != selected):
            return plan
        keep = selected - passed
        return s.repack(keep, plan) if keep else []

    def close(self):
        if self.zygote is not None:
            self.zygote.close()
            return getattr(self.zygote, "total_cpu", None)


def _cpu():
    t = os.times()
    return t.user + t.system + t.children_user + t.children_system
