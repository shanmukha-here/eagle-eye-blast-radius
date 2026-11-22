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
        self.execution_cpu = 0.0

    def cap(self, requested):
        # Leave room for the static selector and stdout after stopping replays.
        available = self.deadline - seconds() + self.execution_cpu - 0.040 * self.remaining_patches
        return max(0.0, min(requested, available))
