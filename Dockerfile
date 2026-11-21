FROM alpine/git:v2.49.1@sha256:c0280cf9572316299b08544065d3bf35db65043d5e3963982ec50647d2746e26 AS source
RUN git init -q /src && git -C /src fetch --depth=1 --no-tags https://github.com/tobymao/sqlglot.git eb11aec4be8cb2122afe713a4816662c3f0fb2fa \
    && git -C /src checkout -q --detach FETCH_HEAD \
    && test "$(git -C /src rev-parse HEAD)" = eb11aec4be8cb2122afe713a4816662c3f0fb2fa \
    && rm -rf /src/.git

FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
ENV PYTHONDONTWRITEBYTECODE=1
RUN python -m pip install --disable-pip-version-check --no-cache-dir --no-deps --index-url https://pypi.org/simple --only-binary=:all: python-dateutil==4.0.0 tzdata==2026.6
COPY --from=source /src/sqlglot /base/sqlglot
COPY --from=source /src/tests /base/tests
COPY --from=source /src/sqlglotc /base/sqlglotc
COPY --from=source /src/*.md /src/pyproject.toml /src/setup.cfg /base/
RUN python -m compileall -q /base/sqlglot /base/tests
WORKDIR /app
COPY impact.py refine.py import_check.py dependencies.py base.z details.z access.z flow.py reach.py observed.py observers.z flow.z checkpoint.py optimizer_probe.py replay_index.z query_lines.z parser_probe.py miniatures.py stress_guard.py short_probe.py startup.py startup.z stress.z memory_index.z value_observations.py query_values.z ./
COPY tools/build_tpch_sql.py /build_tpch_sql.py
RUN PYTHONHASHSEED=0 PYTHONPATH=/app:/base python /build_tpch_sql.py /app/tpch_sql.json
COPY tools/build_replays.py /build_replays.py
RUN PYTHONHASHSEED=0 PYTHONPATH=/app:/base python /build_replays.py /app/replay_index.z /app/optimizer_inputs
COPY requirements.lock /app/requirements.lock
RUN python -m pip install --disable-pip-version-check --no-cache-dir --no-deps --index-url https://pypi.org/simple --only-binary=:all: -r /app/requirements.lock
ENV SKIP_INTEGRATION=0 OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 PYTHON_CPU_COUNT=2
COPY query_sentinel.py memo_witness.py ./
RUN PYTHONHASHSEED=0 PYTHONPATH=/app:/base python query_sentinel.py
COPY observation_file.py ./
COPY tools/build_project_readers.py /build_project_readers.py
RUN PYTHONPATH=/app python /build_project_readers.py /base /app
COPY tpcds_replay.py ./
COPY tools/build_query_gate.py /build_query_gate.py
RUN PYTHONPATH=/app python /build_query_gate.py /app
COPY receiver_edits.py receiver.index ./
COPY registry_guard.py ./
COPY precision_index.py registry_keys.py ./
COPY tools/trace_precision.py tools/build_precision_index.py /precision-tools/
COPY tools/precision_startup.py /precision-tools/sitecustomize.py
RUN mkdir /precision-trace \
    && cd /base \
    && PRECISION_TRACE=/precision-trace PYTHONHASHSEED=0 PYTHON_CPU_COUNT=4 \
       PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONPATH=/precision-tools:/app \
       python -B -m pytest -q --assert=plain -p no:cacheprovider -p trace_precision tests \
    && PYTHONPATH=/app python -B /precision-tools/build_precision_index.py /precision-trace /app /base \
    && rm -r /precision-trace
COPY precision.py precision_body.py ./
COPY long_cases.py ./
COPY tools/build_long_cases.py /build_long_cases.py
RUN cd /base \
    && PYTHONHASHSEED=0 PYTHONPATH=/app:/base python -B /build_long_cases.py /base /app
COPY decision.py selector.py narrow.py fixture_reads.json long_gate.py parser_signal.py cpu_budget.py surface.py definition_metadata.py ./
COPY observation_index.py value_observations.py values.z values_eq.z ready_imports.json fixture_inputs.json ./
COPY tools/build_observation_file.py /build_observation_file.py
RUN PYTHONPATH=/app python /build_observation_file.py /app
COPY exact_tests.py module_topology.py isolated_cpu.bin ./
COPY tools/build_unit_ready.py /build_unit_ready.py
RUN SKIP_INTEGRATION=0 PYTHONPATH=/app:/base python /build_unit_ready.py /app/base.z /app/unit_ready.json /base
COPY reference_replay.py reference_reads.json ./
RUN PYTHONHASHSEED=0 PYTHONPATH=/app:/base python reference_replay.py build
COPY incumbent/ ./incumbent/
RUN ln -s ../replay_index.z incumbent/replay_index.z \
    && ln -s ../optimizer_inputs incumbent/optimizer_inputs
COPY risk_features.py risk_model.py risk_policy.py risk_model.bin ./
COPY tools/build_risk_names.py /build_risk_names.py
RUN PYTHONPATH=/app python -B /build_risk_names.py /base /app/base.z /app/risk_names.index
COPY recovery.py ./
COPY tools/build_import_closure.py /build_import_closure.py
RUN PYTHONPATH=/app python -B /build_import_closure.py /base /app
COPY cold_import.py ./
COPY tools/build_cold_import_index.py /build_cold_import_index.py
RUN PYTHONHASHSEED=0 PYTHONPATH=/app python -B /build_cold_import_index.py /base /app/cold_import.index /app/import_check.py
CMD ["python", "/app/selector.py"]
