"""Share a CPU allowance across optional clean-base replay checks."""

from resource import getrusage, RUSAGE_CHILDREN, RUSAGE_SELF


def seconds():
    parent = getrusage(RUSAGE_SELF)
    children = getrusage(RUSAGE_CHILDREN)
    return parent.ru_utime + parent.ru_stime + children.ru_utime + children.ru_stime


class CpuBudget:
    def __init__(self, started, count):
        self.deadline = started + 0.27 * count
        self.remaining_patches = count

    def cap(self, requested):
        # Leave room for the static selector and stdout after stopping replays.
        available = self.deadline - seconds() - 0.040 * self.remaining_patches
        return max(0.0, min(requested, available))

    def import_cap(self, requested):
        # A cold import verifies that the observer set is sufficient. Reserve a
        # short window for it even if the cheaper tests exhausted replay time.
        return min(requested, max(0.40, self.cap(requested)))
