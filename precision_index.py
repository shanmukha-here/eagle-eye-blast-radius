"""Read dense, fixed-width coverage masks from a build-time memory map."""
from mmap import ACCESS_READ, mmap
from struct import Struct

HEADER = Struct("<8sII")
ITEM = Struct("<II")
MAGIC = b"BLASTPR1"


def build(path, values, width):
    entries = bytearray()
    masks = bytearray()
    strings = bytearray()
    for key, mask in sorted(values.items()):
        label = key.encode()
        entries.extend(ITEM.pack(len(strings), len(label)))
        strings.extend(label)
        masks.extend(mask.to_bytes(width, "little"))
    with path.open("wb") as output:
        output.write(HEADER.pack(MAGIC, len(values), width))
        output.write(entries)
        output.write(masks)
        output.write(strings)


class PrecisionIndex:
    def __init__(self, path):
        with path.open("rb") as file:
            self.mapping = mmap(file.fileno(), 0, access=ACCESS_READ)
        magic, self.count, self.width = HEADER.unpack_from(self.mapping)
        if magic != MAGIC or not self.width:
            raise ValueError("Invalid precision trace index")
        self.masks = HEADER.size + self.count * ITEM.size
        self.strings = self.masks + self.count * self.width

    def mask(self, key):
        sought = key.encode()
        first, last = 0, self.count
        while first < last:
            slot = (first + last) // 2
            at, size = ITEM.unpack_from(self.mapping, HEADER.size + ITEM.size * slot)
            head = self.strings + at
            current = self.mapping[head:head + size]
            if current < sought:
                first = slot + 1
            elif current > sought:
                last = slot
            else:
                start = self.masks + slot * self.width
                return int.from_bytes(self.mapping[start:start + self.width], "little")
        return 0
