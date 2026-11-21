"""Read a flat, memory-mapped code-to-assertion index."""
import marshal
from mmap import ACCESS_READ, mmap
from struct import Struct


MAGIC = b"BLASTAS1"
HEADER = Struct("<8sIII")
KEY = Struct("<IIII")
UNIT = Struct("<IIIII")
REPLAYABLE = 1
ELIGIBLE = 2


class AssertionIndex:
    def __init__(self, path):
        with path.open("rb") as f:
            self.data = mmap(f.fileno(), 0, access=ACCESS_READ)
        magic, self.key_count, edge_count, unit_count = HEADER.unpack_from(self.data)
        if magic != MAGIC:
            self.data.close()
            raise ValueError("Unsupported assertion index")
        self.edges = HEADER.size + self.key_count * KEY.size
        self.units = self.edges + edge_count * 4
        self.blob = self.units + unit_count * UNIT.size

    def read(self, start, size):
        offset = self.blob + start
        return self.data[offset:offset + size]

    def hits(self, name):
        target = name.encode()
        low, high = 0, self.key_count
        while low < high:
            middle = (low + high) // 2
            start, size, first, count = KEY.unpack_from(self.data, HEADER.size + middle * KEY.size)
            key = self.read(start, size)
            if key < target:
                low = middle + 1
            elif key > target:
                high = middle
            else:
                return Struct(f"<{count}I").unpack_from(self.data, self.edges + first * 4)
        return ()

    def details(self, number):
        return UNIT.unpack_from(self.data, self.units + number * UNIT.size)

    def test(self, number):
        start, size, _, _, _ = self.details(number)
        return self.read(start, size).decode()

    def flags(self, number):
        return self.details(number)[-1]

    def case(self, number):
        _, _, start, size, _ = self.details(number)
        return marshal.loads(self.read(start, size))
