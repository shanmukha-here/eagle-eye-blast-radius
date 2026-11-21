"""Emit one valid, empty schedule for every input patch."""
import json
from pathlib import Path


if __name__ == "__main__":
    for patch in sorted(Path("/data/commits").glob("*.patch")):
        print(json.dumps({"commit": patch.stem, "workers": []}, separators=(",", ":")))
