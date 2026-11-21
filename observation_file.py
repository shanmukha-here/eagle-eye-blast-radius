"""Memory-mapped tables of clean-pin observations."""

from mmap import ACCESS_READ, mmap
from struct import Struct

HEADER = Struct("<8sIIII")
KEY = Struct("<IIII")
RECORD = Struct("<IIII")
VALUE = Struct("<BII")
MAGIC = b"BLASTOB1"
UNOBSERVED = (1 << 32) - 1


def build(path, tables):
    """Write sorted keys, fixed-width records and a shared byte arena."""
    keys = bytearray()
    rows = bytearray()
    values = bytearray()
    arena = bytearray()
    known = {}

    def store(data):
        if data not in known:
            known[data] = len(arena)
            arena.extend(data)
        return known[data], len(data)

    def scalar(value):
        if value is None:
            return 0, 0, 0
        if type(value) is bool:
            return 1, int(value), 0
        if type(value) is int:
            return 2, *store(value.to_bytes((value.bit_length() + 8) // 8, "little", signed=True))
        if type(value) is str:
            return 3, *store(value.encode("utf-8", errors="surrogatepass"))
        raise TypeError(f"Unexpected observation type {type(value)}")

    for name, records in sorted(tables.items()):
        first = len(rows) // RECORD.size
        for row, mask in records:
            offset = len(values) // VALUE.size
            if row is not None:
                for value in row:
                    values.extend(VALUE.pack(*scalar(value)))
            mask_bytes = mask.to_bytes((mask.bit_length() + 7) // 8, "little")
            rows.extend(RECORD.pack(*store(mask_bytes), offset, UNOBSERVED if row is None else len(row)))
        keys.extend(KEY.pack(*store(name.encode()), first, len(rows) // RECORD.size))
    with path.open("wb") as target:
        target.write(HEADER.pack(MAGIC, len(keys) // KEY.size, len(keys), len(rows), len(values)))
        for part in (keys, rows, values, arena):
            target.write(part)


class ObservationFile:
    def __init__(self, path):
        with path.open("rb") as source:
            self.data = mmap(source.fileno(), 0, access=ACCESS_READ)
        magic, self.count, key_size, record_size, value_size = HEADER.unpack_from(self.data)
        if magic != MAGIC or key_size != self.count * KEY.size:
            self.data.close()
            raise ValueError("Invalid observation index")
        self.rows = HEADER.size + key_size
        self.values = self.rows + record_size
        self.arena = self.values + value_size

    def bytes_at(self, offset, size):
        start = self.arena + offset
        return self.data[start:start + size]

    def scalar(self, index):
        typ, offset, size = VALUE.unpack_from(self.data, self.values + index * VALUE.size)
        if typ == 0:
            return None
        if typ == 1:
            return bool(offset)
        if typ == 2:
            return int.from_bytes(self.bytes_at(offset, size), "little", signed=True)
        if typ == 3:
            return self.bytes_at(offset, size).decode("utf-8", errors="surrogatepass")
        raise ValueError(f"Unknown scalar tag {typ}")

    def get(self, name):
        sought = name.encode()
        low, high = 0, self.count
        while low < high:
            middle = (low + high) // 2
            offset, size, start, end = KEY.unpack_from(self.data, HEADER.size + middle * KEY.size)
            key = self.bytes_at(offset, size)
            if key < sought:
                low = middle + 1
            elif key > sought:
                high = middle
            else:
                return tuple(self.record(index) for index in range(start, end))
        return ()

    def record(self, index):
        offset, size, first, count = RECORD.unpack_from(self.data, self.rows + index * RECORD.size)
        values = (None if count == UNOBSERVED else
                  tuple(self.scalar(i) for i in range(first, first + count)))
        return values, int.from_bytes(self.bytes_at(offset, size), "little")
