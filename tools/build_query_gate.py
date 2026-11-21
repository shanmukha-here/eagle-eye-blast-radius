"""Index clean query coverage and make verified snapshots readable without decompression."""
import json
from pathlib import Path
import sys
import zlib

from observation_file import build


def main(directory):
    replay = json.loads(zlib.decompress((directory / "replay_index.z").read_bytes()))
    boundary = json.loads((directory / "tpcds_sql.json").read_bytes())
    data = {"boundary:" + path: ((None, 1),) for path in boundary["boundary_files"]}

    def mask(ids):
        return sum(1 << i for i in ids)

    modules = {}
    for site, (optimizer, executor) in replay["uses"].items():
        optimize_mask, execute_mask = mask(optimizer), mask(executor)
        data["code:" + site] = (((optimize_mask, execute_mask), 0),)
        path = site.partition(":")[0]
        old_optimizer, old_executor = modules.get(path, (0, 0))
        modules[path] = old_optimizer | optimize_mask, old_executor | execute_mask
    for path, coverage in modules.items():
        data["module:" + path] = ((coverage, 0),)
    line_index = json.loads(zlib.decompress((directory / "query_lines.z").read_bytes()))
    for location, index in line_index["locations"].items():
        optimizer, executor = line_index["pool"][index]
        if optimizer >= 0 and executor >= 0:
            data["line:" + location] = (((optimizer, executor), 0),)
    inputs = directory / "optimizer_inputs"
    verified = 0
    for i, (key, check, opt) in enumerate(replay["cases"]):
        if (inputs / (key + ".z")).is_file():
            data[f"case:{i}"] = (((key, check, opt), 0),)
            verified += 1
    for path in inputs.glob("*.z"):
        flat = path.with_suffix(".pkl")
        if not flat.is_file():
            flat.write_bytes(zlib.decompress(path.read_bytes()))
    build(directory / "query_gate.index", data)
    print(f"indexed {verified} verified query snapshots and {len(data)} query observations")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
