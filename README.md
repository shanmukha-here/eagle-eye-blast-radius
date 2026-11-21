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

## Dependencies and collection failures

A separate clean-base trace records dialect field reads on instances and classes. It follows inheritance, reflective reads and import-time use. Simple scalar field edits may use that narrower set, with the original cold-import check. An isolated collection failure in a numbered test module includes the file's numbered tests, its transitive importers and source readers. Syntax errors in production modules retain the full fallback.

For a new top-level package module containing only constants or a docstring, the selector checks existing names, module lookups, path probes and directory readers. Entries with executable expressions, test discovery names, package collisions, unknown layouts or additional changed files keep the broad fallback.

Regenerate the additional map with a fresh startup directory and the same locked environment as the other traces. Set TOOLS, ENTRY and TRACES as above.

```sh
mkdir -p "$TRACES"/{access,access-startup}
echo 'from trace_access import install; install()' > "$TRACES/access-startup/sitecustomize.py"
TRACE_OUTPUT="$TRACES/access" PYTHONPATH="$TRACES/access-startup:$TOOLS:$PWD" \
    python -m pytest -q -p trace_access -p pytest_timeout --timeout=160
python "$TOOLS/make_access.py" "$TRACES/access" "$PWD" "$ENTRY/base.z" "$ENTRY/access.z"
```

## Local leaderboard

Run `./localboard.py` to score HEAD or `./localboard.py <revision>` to replay an earlier commit. It builds the Git archive, runs the exact image three times per dataset and prints the median score and calibrated board prediction. It needs a Linux Docker host with systemd and cgroup v2. A paused sibling holds open a private cgroup slice so the harness can measure CPU across the full container lifetime, including subprocesses.

Supply an external `--config` JSON file, defaulting to `../localboard.json`. Its `kit` field points to the official scorer directory and `scorer_sha256` pins the scorer file. Each entry under `datasets` has a `path` and a `sha256` from `localboard.fingerprint`. The entries named `public` and `blind` supply the prediction and sensitivity check. Extra datasets are reported separately. The `calibration` field names the external Markdown ledger. Its seven table columns are commit, public local seconds, blind local seconds, predicted seconds at submission, actual board seconds, public ratio and blind ratio. Ratios are recomputed from the numeric scores. Data and calibration records stay outside the image and checkout.

An optional SSH relay config accepts `ssh_host`, `remote_dir`, `calibration` and an optional `ssh` executable. It stages the frozen archive, harness and current ledger in a temporary directory on the configured Docker host. The host's dataset config is read from `remote_dir/localboard.json`.
