FROM alpine/git:v2.49.1@sha256:c0280cf9572316299b08544065d3bf35db65043d5e3963982ec50647d2746e26 AS source
RUN git init -q /src && git -C /src fetch --depth=1 --no-tags https://github.com/tobymao/sqlglot.git eb11aec4be8cb2122afe713a4816662c3f0fb2fa \
    && git -C /src checkout -q --detach FETCH_HEAD \
    && test "$(git -C /src rev-parse HEAD)" = eb11aec4be8cb2122afe713a4816662c3f0fb2fa \
    && rm -rf /src/.git

FROM scratch AS maps
ADD build-maps.tar.xz /frozen/

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
COPY requirements.lock /app/requirements.lock
RUN python -m pip install --disable-pip-version-check --no-cache-dir --no-deps --index-url https://pypi.org/simple --only-binary=:all: -r /app/requirements.lock
ENV SKIP_INTEGRATION=0 OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 PYTHON_CPU_COUNT=2
COPY query_sentinel.py memo_witness.py ./
COPY observation_file.py ./
COPY tpcds_replay.py ./
COPY receiver_edits.py receiver.index ./
COPY registry_guard.py ./
COPY precision_index.py registry_keys.py ./
COPY precision.py precision_body.py ./
COPY long_cases.py narrow.py cpu_budget.py ./
COPY decision.py selector.py fixture_reads.json long_gate.py parser_signal.py surface.py definition_metadata.py ./
COPY observation_index.py value_observations.py values.z values_eq.z ready_imports.json fixture_inputs.json ./
COPY exact_tests.py module_topology.py isolated_cpu.bin ./
COPY reference_replay.py reference_reads.json ./
COPY incumbent/ ./incumbent/
COPY risk_features.py risk_model.py risk_policy.py risk_model.bin ./
COPY recovery.py ./
COPY cold_import.py ./

# These maps were generated from the same pinned source and test wheels.
COPY --from=maps /frozen/app/ /app/

CMD ["python", "/app/selector.py"]
