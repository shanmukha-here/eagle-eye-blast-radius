import ast
import math
from pathlib import Path
from types import SimpleNamespace

from risk_features import RiskFeatures, member_change
from risk_policy import MISS_MS, charge, choose, hazard
from selector import Scheduler


def test_one_miss_charge_for_the_entire_dropped_set():
    amount = charge(hazard(0.04) + hazard(0.02))
    assert math.isclose(amount, MISS_MS * (1 - 0.96 * 0.98))


def test_cheaper_expected_plan_uses_the_actual_worker_cost():
    rows = [{"number": 0, "id": "long", "seconds": 30},
            {"number": 1, "id": "small", "seconds": 1}]
    scheduler = Scheduler(rows, ["long", "small"])
    plan, risk = choose(scheduler, [[0], [1]], {0: 0.01, 1: 0.5})
    assert plan == [[1]]
    assert math.isclose(risk, 0.01)
    assert choose(scheduler, [[0], [1]], {0: 0.25, 1: 0.5}) == ([[0], [1]], 0)


def test_unseen_formats_do_not_get_risk_features():
    classifier = RiskFeatures(SimpleNamespace(root=Path("/base")), None, None, None, [])
    for change in ("GIT binary patch\n", "@@ -1 +1 @@\r\n-old\r\n+new\r\n",
                   "diff --git a/setup.cfg b/setup.cfg\n--- a/setup.cfg\n+++ b/setup.cfg\n@@ -1 +1 @@\n-old\n+new\n",
                   "diff --git a/sqlglot/a.py b/sqlglot/b.py\nrename from sqlglot/a.py\nrename to sqlglot/b.py\n"):
        assert classifier.analyze(change) is None


def test_hybrid_prices_a_whole_worker_on_a_packing_plateau():
    rows = [{"number": i, "id": str(i), "seconds": 0.05} for i in range(2)]
    scheduler = Scheduler(rows, ["0", "1"])
    p = {0: 0.001, 1: 0.001}
    assert choose(scheduler, [[0, 1]], p, "greedy")[0] == [[0, 1]]
    assert choose(scheduler, [[0, 1]], p, "hybrid")[0] == []


def test_member_risk_form_requires_an_isolated_undecorated_definition():
    source = "class Rule:\n    flag = True\n    def build(self, count: int): return count\n"
    unchanged = "class Rule:\n    flag = True\n"
    assert member_change(ast.parse(source), ast.parse(unchanged)) == ("Rule", "build")
    assert member_change(ast.parse(source), ast.parse(unchanged + "import os\n")) is None
    decorated = source.replace("    def build", "    @staticmethod\n    def build")
    assert member_change(ast.parse(decorated), ast.parse(unchanged)) is None
