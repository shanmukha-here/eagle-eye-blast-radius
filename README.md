# Eagle Eye test selector

The container reads unified diffs and the numbered test manifest from `/data`. It reconstructs each change against the pinned sqlglot tree, compiles edited Python source to check validity, and uses clean-base coverage to select tests. It schedules tests using their supplied durations and adds extra checks in unused worker capacity.

## How tests are selected

- Fixture edits select observed readers. Python edits with identical syntax trees retain tests that read the source or inspect Python source positions. A change of encoding widens the selection.
- A replacement within a function uses the original statement's executed lines. A changed short-circuit condition includes the start of the predicate. Changed lexical bindings, unobserved lines and unsupported statement changes retain the enclosing function's callers.
- For a single comparison edit, the selector can distinguish identity from equality using the concrete types seen at that comparison in each test. It clears a context only when the two operators agree for all its observed types. This covers comparisons with `None` for known types and comparisons between canonical members of a verified enum. Unknown operand types and shared setup observations retain their tests.
- A literal value changed in an existing class field can select observed receivers, inherited dialect users and source references. The selector first imports the patched module and initializes the built-in dialect registry in a fresh, bounded interpreter. An import failure or timeout selects the full suite. Changes outside the supported field shapes also select the full suite.
- Edited test methods retain direct and inherited callers and tests in dependent modules. Unrecognized diffs, unresolved dependencies and unknown test IDs are handled conservatively.

The default image carries two compressed maps. They contain pytest node IDs and observations of the unchanged public source. No patch inputs, test durations, patch identifiers or failure labels are used to build them.

## Regenerating the coverage maps

Use the pinned sqlglot source and CPython image named in the Dockerfile. Install the organizer's matching package lock in the profiling environment. Run from the root of the clean checkout. The commands below assume absolute paths for `TOOLS` (this repository's tools directory), `ENTRY` (this repository) and `TRACES` (a new private directory).

```sh
export SKIP_INTEGRATION=0 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHON_CPU_COUNT=3
export OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1
export PYTHONPATH="$TOOLS:$PWD"

mkdir -p "$TRACES"/{calls,lines,receivers,operands,startup}
TRACE_OUTPUT="$TRACES/calls" python -m pytest -q -p trace_base -p pytest_timeout --timeout=160
TRACE_OUTPUT="$TRACES/lines" python -m pytest -q -p trace_lines -p pytest_timeout --timeout=160
TRACE_OUTPUT="$TRACES/receivers" python -m pytest -q -p trace_types -p pytest_timeout --timeout=160

# Install operand observation before importing sqlglot in both parent and spawned interpreters.
echo 'from trace_operands import install; install()' > "$TRACES/startup/sitecustomize.py"
TRACE_OUTPUT="$TRACES/operands" PYTHONPATH="$TRACES/startup:$PYTHONPATH" \
    python -m pytest -q -p trace_operands -p pytest_timeout --timeout=160

python "$TOOLS/make_map.py" "$TRACES/calls" "$ENTRY/base.z"
python "$TOOLS/make_details.py" "$TRACES/lines" "$TRACES/receivers" "$TRACES/operands" \
    "$PWD" "$ENTRY/base.z" "$ENTRY/details.z"
```

Use complete, successful base runs. Pytest IDs follow work into spawned process-pool tasks. Setup and teardown can widen impact to an entire test file. The decision image uses the committed maps and has no pytest dependency.
