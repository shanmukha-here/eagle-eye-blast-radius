"""Cold, bounded decision probes. Unclassified changes retain their original mask."""
import json
import os
import subprocess
import sys
import zlib
from pathlib import Path

from optimizer_probe import cost, local_edit
from stress_guard import eligible, same_full_predicates


CORE = frozenset({"sqlglot/parser.py", "sqlglot/tokens.py", "sqlglot/tokenizer_core.py"})


class Miniatures:
    def __init__(self, ids, root, directory):
        self.directory = Path(directory).resolve()
        self.root = Path(root).resolve()
        self.memory = json.loads(zlib.decompress((self.directory / "memory_index.z").read_bytes()))
        self.stress = json.loads(zlib.decompress((self.directory / "stress.z").read_bytes()))
        self.tests = {name: 1 << ids.index(node) for name, node in {
            "parser": "tests/test_parser.py::TestParser::test_max_nodes",
            "memory": "tests/test_generator.py::TestGenerator::test_generator_memory",
            "optimized": "tests/test_executor.py::TestExecutor::test_optimized_tpch",
            "execute": "tests/test_executor.py::TestExecutor::test_execute_tpch",
        }.items() if node in ids}
        self.used = 0.0

    def check(self, mask, scheduler, kind, path, source, cases=()):
        bit = self.tests.get(kind, 0)
        if not mask & bit:
            return mask
        reduced = mask & ~bit
        gain = (cost(scheduler, mask) - cost(scheduler, reduced)) / 1000
        timeout = min(2.0, gain * 0.6)
        if timeout < 0.7 or self.used + timeout > 40:
            return mask
        self.used += timeout
        script = "parser_probe.py" if kind == "parser" else "short_probe.py"
        cmd = [sys.executable, "-B", str(self.directory / script), str(self.root), path]
        if kind != "parser":
            cmd += [kind, *cases]
        try:
            child = subprocess.run(cmd, input=source.encode(), stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, timeout=timeout,
                                   env=dict(os.environ, PYTHONHASHSEED="0", PYTHONPATH=""))
        except (OSError, subprocess.TimeoutExpired):
            return mask
        return reduced if child.returncode == 0 else mask

    def select(self, mask, scheduler, path, source, key, old, new, readers):
        if not scheduler or path.endswith("/__init__.py"):
            return mask
        # The H queries touch the parser only after setup. Restrict the code
        # edit to token operands; fixture loading and worker setup stay identical.
        h = self.tests.get("execute", 0) | self.tests.get("optimized", 0)
        if mask & h and not readers & h and eligible(path, old[3], new[3]):
            for kind in ("execute", "optimized"):
                mask = self.check(mask, scheduler, kind, path, source)
        if (mask & self.tests.get("memory", 0) and not readers & self.tests.get("memory", 0)
                and path.startswith("sqlglot/generators/") and local_edit(old[3], new[3])):
            location = f"{path}:{old[0]}:{'.'.join(key[:-1])}"
            cases = self.memory.get(location)
            if cases and len(cases) <= 4:
                mask = self.check(mask, scheduler, "memory", path, source, cases)
        return mask


    def protect_core(self, mask, scheduler, path, source, before, after):
        """A semantic edit to the core grammar needs a positive full-width witness."""
        original, scopes = before
        revised, new_scopes = after
        if original == revised and scopes.keys() == new_scopes.keys():
            keys = [k for k in scopes if scopes[k][2] != new_scopes[k][2]]
            if not keys:
                return mask
            if len(keys) == 1:
                key = keys[0]
                a, b = scopes[key][3], new_scopes[key][3]
                if eligible(path, a, b) and same_full_predicates(a, b, self.stress):
                    return self.check(mask | self.tests.get("parser", 0), scheduler, "parser", path, source)
        return mask | self.tests.get("parser", 0)
