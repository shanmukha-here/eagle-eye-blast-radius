# Eagle Eye test selector

The container reads the mounted test manifest and unified diffs from `/data`. It uses function calls and file reads observed on the unchanged sqlglot source tree to select tests. It reconstructs modified Python text against that tree, checks whether it still compiles, and compares definitions and function bodies. Unknown patch shapes and unresolved dependencies select the whole manifest. The scheduler tries up to eight worker counts and fills unused capacity with extra checks.

The map contains public pytest node IDs and coverage bitsets. It contains no patch inputs, timings, patch identifiers or failure labels. Keys are relative source paths and Python code objects' first lines. Line zero records observed file reads. Collection-time activity covers the full suite, and setup/teardown activity covers the associated test file. Process-pool tasks carry the submitting test context.

## Regenerating the map

Use the pinned source and CPython image named in the Dockerfile. In the offline profiling environment install the organizer's matching package lock. Set `SKIP_INTEGRATION=0`, `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`, `PYTHON_CPU_COUNT=3`, `OPENBLAS_NUM_THREADS=1` and `OMP_NUM_THREADS=1`. From the unchanged sqlglot checkout, add the checkout and this repository's `tools` directory to `PYTHONPATH`. Point `TRACE_OUTPUT` at a new empty directory. Run:

```sh
python -m pytest -q -p trace_base -p pytest_timeout --timeout=100
python /path/to/tools/make_map.py "$TRACE_OUTPUT" /path/to/base.z
```

Use a successful, complete clean-base run. The official build fetches the fixed source commit and copies the committed map. It requires neither pytest nor a test run at decision time.
