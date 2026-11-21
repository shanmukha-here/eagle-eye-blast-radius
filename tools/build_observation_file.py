"""Convert the clean observation maps to flat tables at image build time."""
import json
from pathlib import Path
import sys
import zlib

from observation_file import build


def load(path):
    return json.loads(zlib.decompress(path.read_bytes()))


def arrays(directory):
    baseline = load(directory / "values.z")
    for key, bits in baseline["defaults"].items():
        yield "d" + key, ((None, int(bits, 16)),)
    for key, records in baseline["values"].items():
        yield "v" + key, ((None if row is None else tuple(value for _, value in row), int(bits, 16))
                          for row, bits in records)
    for key, records in load(directory / "values_eq.z")["values"].items():
        yield "c" + key, ((tuple(value for _, value in row), int(bits, 16)) for row, bits in records)


if __name__ == "__main__":
    directory = Path(sys.argv[1])
    build(directory / "values.index", dict(arrays(directory)))
