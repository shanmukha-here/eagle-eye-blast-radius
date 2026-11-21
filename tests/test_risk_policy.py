import math
from pathlib import Path
from types import SimpleNamespace

from risk_features import RiskFeatures
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
