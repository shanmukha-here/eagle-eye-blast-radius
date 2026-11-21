import difflib
import json
import tempfile
import unittest
from pathlib import Path

from impact import Impact
from selector import Scheduler


class ImpactTests(unittest.TestCase):
    name = "sqlglot/example.py"
    source = "def calculate(x):\n    return x + 1\n\ndef calculate(y):\n    return y * 2\n"

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / self.name
        path.parent.mkdir()
        path.write_text(self.source)
        self.impact = Impact(directory.name, {
            "ids": ["first", "second", "source-reader", "other"],
            "files": {self.name: {"0": "4", "1": "1", "4": "2"}},
            "visitors": {self.name: "3"},
        })

    def patch(self, new):
        header = f"diff --git a/{self.name} b/{self.name}\n"
        return header + "".join(difflib.unified_diff(
            self.source.splitlines(True), new.splitlines(True),
            fromfile="a/" + self.name, tofile="b/" + self.name,
        ))

    def test_repeated_definition_has_its_own_coverage(self):
        self.assertEqual(self.impact.select(self.patch(self.source.replace("x + 1", "x - 1"))), 5)
        self.assertEqual(self.impact.select(self.patch(self.source.replace("y * 2", "y / 2"))), 6)

    def test_syntax_and_definition_changes_are_conservative(self):
        for new in (self.source.replace("y * 2", "y +"), self.source.replace("(x)", "(x=3)")):
            self.assertEqual(self.impact.select(self.patch(new)), 15)

    def test_context_mismatch_and_unsupported_diff_use_full_set(self):
        patch = self.patch(self.source.replace("y * 2", "y / 2"))
        self.assertEqual(self.impact.select(patch.replace("-    return y * 2", "-    return z")), 15)
        self.assertEqual(self.impact.select(patch + "diff --git a/a b/b\n"), 15)

    def test_comment_includes_source_readers(self):
        self.assertEqual(self.impact.select(self.patch("# explanation\n" + self.source)), 7)


class SchedulingTests(unittest.TestCase):
    def test_mapped_ids_are_matched_by_name_and_new_ids_always_run(self):
        tests = [{"number": 4, "id": "second", "seconds": 1.0},
                 {"number": 0, "id": "new", "seconds": 0.0},
                 {"number": 3, "id": "first", "seconds": 0.1}]
        schedule = json.loads(Scheduler(tests, ["first", "second"]).schedule(1))
        self.assertEqual({t for worker in schedule for t in worker}, {0, 3})

    def test_insurance_uses_slack_and_preserves_empty_plans(self):
        tests = [{"number": n, "id": str(n), "seconds": duration}
                 for n, duration in enumerate((0.010, 0.009, 0.000, 0.001, 0.001, 0.000))]
        scheduler = Scheduler(tests, [str(n) for n in range(len(tests))])
        original = [[0], [1]]
        insured = scheduler.insure(original, (1 << 5) - 1, {4})
        self.assertEqual(insured, [[0], [1, 2, 3]])
        self.assertEqual(scheduler.cost(insured), scheduler.cost(original))
        self.assertEqual(original, [[0], [1]])
        self.assertEqual(scheduler.insure([], 63, set()), [])


class StartupTests(unittest.TestCase):
    def test_worker_is_added_only_for_a_strict_cost_improvement(self):
        counts = [2.0, 2.001]
        for load, expected in zip(counts, [1, 2]):
            tests = [{"number": i, "id": str(i), "seconds": load} for i in range(2)]
            schedule = Scheduler(tests, ["0", "1"])
            self.assertEqual(len(json.loads(schedule.schedule(3, pad=False))), expected)
