"""Index function entries during successful forced imports of clean modules."""

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import subprocess
import sys

from precision_index import build


def main(root, output, helper, workers=4):
    def trace(file):
        path = file.relative_to(root).as_posix()
        try:
            completed = subprocess.run(
                [sys.executable, "-IBS", str(helper), str(root), path, "--trace"],
                input=file.read_bytes(), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                timeout=6, check=True,
            )
            return path, json.loads(completed.stdout)
        except (OSError, subprocess.SubprocessError, ValueError):
            return path, None

    files = [file for file in (root / "sqlglot").rglob("*.py") if file.name != "__init__.py"]
    entries = {}
    indexed = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for path, calls in pool.map(trace, files):
            if calls is not None:
                indexed += 1
                entries[path] = 1
                entries.update({f"{path}:{name}": 1 for name in calls})
    build(output, entries, 1)
    print(f"Indexed cold imports for {indexed} of {len(files)} modules", flush=True)


if __name__ == "__main__":
    main(*(Path(value) for value in sys.argv[1:]))
