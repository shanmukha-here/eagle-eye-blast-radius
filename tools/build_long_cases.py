"""Build long-test line masks and clean input costs with one trace per case."""

from collections import defaultdict
from pathlib import Path
import sys
from time import process_time_ns

from long_cases import EXECUTE_DS, EXECUTE_H, Inputs, MEMORY, METHODS, OPTIMIZED_H
from precision_index import build


def trace(base, out):
    root = str(base.resolve()) + "/"
    values = defaultdict(int)
    monitor = sys.monitoring
    tool = 2
    monitor.use_tool_id(tool, "long-test cases")
    method = 0
    bit = 1

    def line(code, lineno):
        path = code.co_filename
        if path.startswith(root + "sqlglot/"):
            source = path[len(root):]
            values[f"{method}:{source}:{lineno}"] |= bit
            values[f"{method}:{source}"] |= bit
        return monitor.DISABLE

    def switch(mask):
        nonlocal bit
        bit = mask
        monitor.restart_events()

    def clock():
        return process_time_ns() // 1_000

    inputs = Inputs()
    signatures = bytearray()
    monitor.register_callback(tool, monitor.events.LINE, line)
    monitor.set_events(tool, monitor.events.LINE)
    try:
        for method in range(len(METHODS)):
            switch(1)
            started = clock()
            count = inputs.prepare(method)
            if method in (EXECUTE_H, EXECUTE_DS, OPTIMIZED_H):
                # Every query may depend on optimized or shared fixture setup.
                # Trace the fixture path once outside the individual queries.
                if inputs.executor is None:
                    setup = clock()
                    inputs.tables()
                    fixture_cpu = clock() - setup
                    started += fixture_cpu
            values[f"count:{method}"] = count
            values[f"setup:{method}"] = max(1, clock() - started)
            if method == EXECUTE_DS:
                values[f"setup:{method}"] += fixture_cpu
            for case in range(count):
                switch(2 << case)
                started = clock()
                try:
                    signature = inputs.run(method, case)
                except AssertionError:
                    # Trace storage itself allocates while tracemalloc is
                    # active. Verify memory tests after removing the observer.
                    if method != MEMORY:
                        raise
                if method == OPTIMIZED_H:
                    signatures.extend(signature)
                    started = clock()
                    inputs.run(method, case, signature)
                values[f"cpu:{method}:{case}"] = max(1, clock() - started)
            print(f"traced {METHODS[method]} ({count} cases)", flush=True)
        switch(1)
        inputs.close()
    finally:
        monitor.set_events(tool, 0)
        monitor.free_tool_id(tool)
    # The uninstrumented memory bound must pass and sets its actual CPU price.
    inputs = Inputs()
    for case in range(inputs.prepare(MEMORY)):
        started = clock()
        inputs.run(MEMORY, case)
        values[f"cpu:{MEMORY}:{case}"] = max(1, clock() - started)
    values["module_coverage"] = 1
    width = (max(values.values()).bit_length() + 7) // 8
    build(out / "long_cases.index", values, width)
    (out / "long_cases.fingerprints").write_bytes(signatures)


if __name__ == "__main__":
    trace(Path(sys.argv[1]), Path(sys.argv[2]))
