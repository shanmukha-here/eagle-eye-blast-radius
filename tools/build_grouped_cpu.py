"""Encode clean-pin CPU measurements in profile-ID order."""

import json
from pathlib import Path
from struct import Struct
import sys
import zlib


def build(profile, measurement, output):
    ids = json.loads(zlib.decompress(profile.read_bytes()))["ids"]
    measurements = json.loads(measurement.read_bytes())["tests"]
    microseconds = Struct("<I")
    with output.open("wb") as result:
        for name in ids:
            row = measurements.get(name)
            result.write(microseconds.pack(max(1, round(row["cpu"] * 1e6)) if row else 0))


if __name__ == "__main__":
    build(*(Path(arg) for arg in sys.argv[1:]))
