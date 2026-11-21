import ast
import math
from pathlib import Path
from types import SimpleNamespace

from risk_features import RiskFeatures, member_binding_changed, member_change, slot_declarations
from risk_model import DOUBLE, FORM_MAGIC, FORM_POLICY, HEADER, MAGIC, NODE, ROOT, RiskModel
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


def test_mmap_models_share_predictions_with_form_specific_pricing(tmp_path):
    binary = tmp_path / "risk_model.bin"
    curve = ROOT.pack(0) + DOUBLE.pack(0.0) + DOUBLE.pack(0.04)
    leaf = NODE.pack(-1, 0, 0, 0.3)
    binary.write_bytes(HEADER.pack(MAGIC, 1, 1, 1, 1, 0.0, 1.0) + curve + leaf)
    original = RiskModel(binary, 1)
    assert original.settings(0) == (1.0, "hybrid")
    binary = tmp_path / "risk_form_model.bin"
    policy = bytes([1]) + FORM_POLICY.pack(2.5, True)
    binary.write_bytes(HEADER.pack(FORM_MAGIC, 1, 1, 1, 1, 0.0, 1.0) + curve + policy + leaf)
    model = RiskModel(binary, 1)
    scale, mode = model.settings(0)
    assert mode == "greedy"
    assert model.raw((0,)) == original.raw((0,))
    assert model.probability((0,), scale) == 0.1


def test_binding_guard_keeps_independent_overrides_but_allows_forwarding():
    before = ast.parse("class Child(Parent):\n    label = 'child'\n")
    changed = "class Child(Parent):\n    label = 'child'\n    def invoke(self, *args, **kwargs):\n"
    forwards = ast.parse(changed + "        return super().invoke(*args, **kwargs)\n")
    overrides = ast.parse(changed + "        return None\n")
    permutes = ast.parse(changed + "        return super().invoke('other', *args, **kwargs)\n")
    assert not member_binding_changed(before, forwards, ("Child", "invoke"))
    assert member_binding_changed(before, overrides, ("Child", "invoke"))
    assert member_binding_changed(before, permutes, ("Child", "invoke"))


def test_binding_guard_detects_enum_and_slot_rewrites():
    original_enum = ast.parse("class State(enum.StrEnum):\n    ACTIVE = 'active'\n")
    revised_enum = ast.parse("class State(enum.StrEnum):\n    ACTIVE = 'changed'\n")
    assert member_binding_changed(original_enum, revised_enum, ("State", "ACTIVE"))
    original_slots = ast.parse("class State:\n    __slots__ = ('active',)\n")
    revised_slots = ast.parse("class State:\n    __slots__ = ('changed',)\n")
    assert slot_declarations(original_slots) != slot_declarations(revised_slots)
