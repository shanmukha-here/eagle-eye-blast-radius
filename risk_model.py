"""Evaluate a small numerical model in the scorer with a memory-mapped binary."""
from bisect import bisect_left
from mmap import ACCESS_READ, mmap
from struct import Struct

MAGIC = b"BLASTRK1"
HEADER = Struct("<8sHHHHdd")
NODE = Struct("<hHHd")
ROOT = Struct("<I")
DOUBLE = Struct("<d")
GREEDY_ONLY = 1 << 15


class RiskModel:
    def __init__(self, path, features):
        with path.open("rb") as source:
            self.mapping = mmap(source.fileno(), 0, access=ACCESS_READ)
        magic, width, trees, bins, self.supported, self.base, self.scale = HEADER.unpack_from(self.mapping)
        if magic != MAGIC or width != features or not trees or not bins:
            raise ValueError("Invalid risk model")
        self.style = "greedy" if self.supported & GREEDY_ONLY else "hybrid"
        self.supported &= ~GREEDY_ONLY
        self.roots = [ROOT.unpack_from(self.mapping, HEADER.size + i * ROOT.size)[0] for i in range(trees)]
        offset = HEADER.size + trees * ROOT.size
        self.cut = [DOUBLE.unpack_from(self.mapping, offset + i * DOUBLE.size)[0] for i in range(bins)]
        offset += bins * DOUBLE.size
        self.upper = [DOUBLE.unpack_from(self.mapping, offset + i * DOUBLE.size)[0] for i in range(bins)]
        self.nodes = offset + bins * DOUBLE.size

    def raw(self, features):
        score = self.base
        for root in self.roots:
            slot = root
            while True:
                feature, left, right, point = NODE.unpack_from(self.mapping, self.nodes + slot * NODE.size)
                if feature < 0:
                    score += point
                    break
                slot = root + (left if features[feature] <= point else right)
        return score

    def probability(self, features):
        return min(0.99, self.scale * self.upper[min(len(self.cut) - 1, bisect_left(self.cut, self.raw(features)))])
