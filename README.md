# Eagle Eye test selector

The container reads unified diffs and the numbered test manifest from `/data`. It reconstructs each change against the pinned sqlglot tree, compiles edited Python source to check validity, and uses clean-base coverage to select tests. It schedules tests using their supplied durations and adds extra checks in unused worker capacity.

## How tests are selected

- Fixture edits select observed readers. Python edits with identical syntax trees retain tests that read the source or inspect Python source positions. A change of encoding widens the selection.
- A replacement within a function uses the original statement's executed lines. A changed short-circuit condition includes the start of the predicate. Changed lexical bindings, unobserved lines and unsupported statement changes retain the enclosing function's callers.
- For a single comparison edit, the selector can distinguish identity from equality using the concrete types seen at that comparison in each test. It clears a context only when the two operators agree for all its observed types. This covers comparisons with `None` for known types and comparisons between canonical members of a verified enum. Unknown operand types and shared setup observations retain their tests.
- A literal value changed in an existing class field can select observed receivers, inherited dialect users and source references. The selector first imports the patched module and initializes the built-in dialect registry in a fresh, bounded interpreter. An import failure or timeout selects the full suite. Changes outside the supported field shapes also select the full suite.
- Edited test methods retain direct and inherited callers and tests in dependent modules. Unrecognized diffs, unresolved dependencies and unknown test IDs are handled conservatively.

The image carries compressed observations from the unchanged public source and its own test suite. These include test IDs, test coverage and fingerprints of clean query results. No patch inputs, test durations, patch identifiers or failure labels are used to build them.

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

## Instruction coverage and bounded query replay

For a change within one function, the selector compares the old and new bytecode along both branches. Tests that reached the first instruction with different behavior stay selected. Incomplete instruction coverage falls back to the previous selection. An independent call trace can identify an uncalled function. Source readers, opaque subprocesses and code-object observers can still require tests. The comparison shortcut requires an isolated change and compatible observed operand types.

The long TPC-DS integration test contains multiple independent upstream queries. A clean parent-and-child trace identifies functions called exclusively inside particular queries. When a body edit touches one of those functions, the selector can repeat the affected work in an isolated interpreter running the patched source. It compares either the complete optimizer expression graph or the ordered table returned by the query. If a probe fails, times out or would cost too much, the long test stays selected. Building the image regenerates typed input tables from the pinned upstream fixtures and checks them against the clean test observations. Only successful comparisons create replay inputs.

Regenerate the instruction and query maps from the same clean checkout and locked profiling environment. Use a fresh startup directory for each plugin.

```sh
export PYTHONHASHSEED=0
mkdir -p "$TRACES"/{flow,flow-start,cases,cases-start}
echo 'from trace_flow import install; install()' > "$TRACES/flow-start/sitecustomize.py"
TRACE_OUTPUT="$TRACES/flow" PYTHONPATH="$TRACES/flow-start:$TOOLS:$ENTRY:$PWD" \
    python -m pytest -q -p trace_flow -p pytest_timeout --timeout=160
python "$TOOLS/make_flow.py" "$TRACES/flow" "$ENTRY/base.z" "$TRACES/operands" \
    "$PWD" "$ENTRY/details.z" "$ENTRY/flow.z"

echo 'from trace_cases import install; install()' > "$TRACES/cases-start/sitecustomize.py"
PROBE_OUTPUT="$TRACES/cases" PYTHONPATH="$TRACES/cases-start:$TOOLS:$ENTRY:$PWD" \
    python -m pytest -q -p trace_cases tests/test_executor.py::TestExecutor::test_execute_tpcds
python "$TOOLS/make_replay_index.py" "$TRACES/cases" "$ENTRY/replay_index.z"
```

## Class values and branch reachability

A separate clean profile observes plain string-valued enums derived from sqlglot’s AutoName. Reading a member value, reverse lookup with the old or new string, and unclassified introspection can each make a test relevant. The selector checks the complete old and new declarations for name or value collisions and retains the cold-import check. Parser, generator and tokenizer configuration flags use observations of class and instance reads, inherited fields and reflective access. A class definition observed by the profiler is required before an absent read can narrow selection. Reads during import cover all tests.

A nested function edit can use the nested function’s callers when its enclosing factory constructs the same closure and signature. To exclude a missing instruction on a conditional path, the selector requires observations at the branch and its alternative and corroborates the skipped arm against the independent line trace. A mixed change to a single production file and existing fixtures can use the union of the fixture readers and the production reachability estimate. The query replay shortcut remains limited to a single production edit.

To rebuild the additional map, use the same clean pinned source, environment and PYTHONHASHSEED as the instruction trace. Run each plugin separately, including its sitecustomize startup in spawned workers.

```sh
for plugin in trace_components trace_enum trace_guards; do
    mkdir -p "$TRACES/$plugin" "$TRACES/$plugin-start"
    echo "from $plugin import install; install()" > "$TRACES/$plugin-start/sitecustomize.py"
    TRACE_OUTPUT="$TRACES/$plugin" PYTHONPATH="$TRACES/$plugin-start:$TOOLS:$ENTRY:$PWD" \
        python -m pytest -q -p "$plugin" -p pytest_timeout --timeout=180
done
python "$TOOLS/make_observers.py" "$TRACES/trace_components" "$TRACES/trace_enum" \
    "$TRACES/trace_guards" "$ENTRY/base.z" "$ENTRY/details.z" "$ENTRY/observers.z"
```

The operand trace recognizes equality methods that return before inspecting values of a different concrete type. Every formerly unresolved test context must be covered by the new observation. Changes in the comparison implementation’s own source file keep the previous rule.

## Local leaderboard

Run `./localboard.py` to score HEAD or `./localboard.py <revision>` to replay an earlier commit. It builds the Git archive, runs the exact image three times per dataset and prints the median score and calibrated board prediction. It needs a Linux Docker host with systemd and cgroup v2. A paused sibling holds open a private cgroup slice so the harness can measure CPU across the full container lifetime, including subprocesses.

Supply an external `--config` JSON file, defaulting to `../localboard.json`. Its `kit` field points to the official scorer directory and `scorer_sha256` pins the scorer file. Each entry under `datasets` has a `path` and a `sha256` from `localboard.fingerprint`. When supplied, the `proxy` dataset supplies the prediction and `shadow` gives a disjoint sensitivity check. The four-column ledger table headed `Matched commit` lists the historical commit, matched local seconds, official board seconds and ratio. Without a proxy, `public` and `blind` use their historical ratios. Extra datasets are reported separately. The optional `record_datasets` list retains the three raw outputs for each named set in the private Docker-host directory. The `calibration` field names the external Markdown ledger. Its original seven table columns are commit, public local seconds, blind local seconds, predicted seconds at submission, actual board seconds, public ratio and blind ratio. Ratios are recomputed from the numeric scores. Data and calibration records stay outside the image and checkout.

An optional SSH relay config accepts `ssh_host`, `remote_dir`, `calibration` and an optional `ssh` executable. It stages the frozen archive, harness and current ledger in a temporary directory on the configured Docker host. The host's dataset config is read from `remote_dir/localboard.json`.

An optional external config field `historical_response` holds separate local and board score pairs for `proxy` and `shadow`, plus a `realized_fraction` between zero and one. The simulator prints both the median-ratio estimate and the forecast fitted to these historical measurements. Below the best measured local score it credits the specified fraction of additional local savings.

The parser replay runs the upstream node-limit assertions in a new interpreter with 100 columns and the original nesting depth. Before clearing a test, it checks that every recorded token pair at the changed match site produces the same decision at full width. Unknown changes, failed probes and expired reservations keep the test.

The executor probes compare the original query inputs to the pinned DuckDB fixtures. The generator memory probe keeps all 100 warm-up and 1,000 measured constructions for each affected dialect. The clean traces include process startup and bind results to pytest test IDs. Build-time fixture fingerprints come from the pinned checkout.

Fallback analysis compares the patched AST with the clean module, including the
conditions around imports. It covers changed functions, definitions with new literal
defaults, and test-module importers. The clean component trace follows Python's
method resolution order when scalar settings are inherited. Reading the names in
a class dictionary through the builtin dir does not read their values. A separate
clean unittest trace records which tests consume fixture lists created by class
setup. That trace applies only when all input statements and the number of paired
fixture fields are preserved. The bounded executor is reserved for wide selections
with enough removable worker time to pay for its work.
