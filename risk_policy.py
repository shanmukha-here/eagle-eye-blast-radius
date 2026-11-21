"""Price an omitted set once per patch and retain the cheapest expected schedule."""
import math

from precision_index import PrecisionIndex
from risk_features import FEATURE_COUNT, RiskFeatures
from risk_model import RiskModel

MISS_MS = 300_000


def hazard(p):
    return -math.log1p(-p)


def charge(h):
    return -MISS_MS * math.expm1(-h)


def choose(scheduler, workers, estimates, style="hybrid", narrow=None):
    current = {n for worker in workers for n in worker}
    original = scheduler.cost(workers)
    if not current or original == 0:
        return workers, 0.0
    hazards = {n: hazard(estimates[n]) for n in current}
    # Even an empty new schedule cannot justify this individual risk.
    limit = -math.log1p(-min(0.999, original / MISS_MS))
    eligible = [n for n in current if hazards[n] < limit]
    if not eligible:
        return workers, 0.0
    duration = scheduler.durations
    ranked = sorted(eligible, key=lambda n: (-duration[n] / max(hazards[n], 1e-9), n))
    best, value, dropped_risk = workers, float(original), 0.0

    def consider(omit, total):
        nonlocal best, value, dropped_risk
        risk = charge(total)
        if risk >= value:
            return
        plan = scheduler.repack(current - omit, workers)
        expected = scheduler.cost(plan) + risk
        if expected + 30 < value:
            best, value, dropped_risk = plan, expected, risk / MISS_MS

    # The individual cost is exact for small sets. Large sets start with the
    # tests most likely to move the peak or eliminate a worker.
    queue = ranked if len(current) <= 16 else ranked[:12]
    if len(current) > 16:
        queue = list(dict.fromkeys(queue + sorted(eligible, key=lambda n: (-duration[n], n))[:6]))
    removed = set()
    total = 0.0
    current_cost = original
    for _ in range(4):
        winner = None
        for n in queue:
            if n in removed:
                continue
            proposed = total + hazards[n]
            extra = charge(proposed) - charge(total)
            plan = scheduler.repack(current - (removed | {n}), workers)
            cost = scheduler.cost(plan)
            saved = current_cost - cost
            if saved <= extra + 30:
                continue
            priority = (saved / max(extra, 1e-9), saved - extra, -n)
            if winner is None or priority > winner[0]:
                winner = priority, n, plan, cost, proposed
        if winner is None:
            break
        _, n, best, current_cost, total = winner
        removed.add(n)
        value = current_cost + charge(total)
        dropped_risk = charge(total) / MISS_MS
        if len(removed) == len(current):
            break

    if style == "hybrid":
        # Packing has plateaus. A group can clear a worker when no individual
        # removal changes its load enough to change the minimum schedule.
        safest = sorted(eligible, key=lambda n: (hazards[n], -duration[n], n))
        for ranked_order in (ranked, safest):
            omitted = set()
            total = 0.0
            for i, n in enumerate(ranked_order, 1):
                total += hazards[n]
                if total >= limit:
                    break
                omitted.add(n)
                if i <= 8 or i & (i - 1) == 0 or i == len(ranked_order):
                    consider(omitted, total)
            if omitted:
                consider(omitted, total)
        if narrow is not None:
            removed = current - narrow
            consider(removed, sum(hazards[n] for n in sorted(removed)))
    return best, dropped_risk


class RiskPolicy:
    def __init__(self, directory, impact, precision, scheduler, tests):
        model = directory / "risk_model.bin"
        names = directory / "risk_names.index"
        self.model = RiskModel(model, FEATURE_COUNT) if model.is_file() else None
        if self.model is None or precision.index is None or not names.is_file():
            self.features = None
        else:
            self.features = RiskFeatures(impact, PrecisionIndex(names), precision.index, scheduler, tests)
        self.scheduler = scheduler
        self.stats = {}

    def apply(self, text, workers, rules=None):
        self.stats = {}
        if not workers or self.features is None or text is None:
            return workers
        selected = sorted(n for group in workers for n in group)
        if any(not self.scheduler.bits[n] for n in selected):
            return workers
        features = self.features.rows(text, selected, rules)
        if features is None:
            return workers
        form, rows = features
        if not self.model.supported & (1 << form):
            return workers
        memo = {}
        p = {}
        for n, row in zip(selected, rows):
            if row not in memo:
                memo[row] = self.model.probability(row)
            p[n] = memo[row]
        narrow = {n for n, row in zip(selected, rows) if any(row[-5:])} if len(selected) >= 512 else None
        plan, risk = choose(self.scheduler, workers, p, self.model.style, narrow)
        if plan is not workers:
            self.stats = dict(risk_form=form, risk_dropped=len(selected) - sum(map(len, plan)),
                              risk_saved_ms=self.scheduler.cost(workers) - self.scheduler.cost(plan),
                              risk_upper=round(risk, 6))
        return plan
