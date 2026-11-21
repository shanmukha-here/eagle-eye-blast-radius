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
RUN python -m compileall -q /base/sqlglot
WORKDIR /app
COPY impact.py refine.py import_check.py dependencies.py base.z details.z access.z flow.py reach.py observed.py observers.z flow.z checkpoint.py optimizer_probe.py replay_index.z query_lines.z parser_probe.py miniatures.py stress_guard.py short_probe.py startup.py startup.z stress.z memory_index.z ./
COPY tools/build_tpch_sql.py /build_tpch_sql.py
RUN PYTHONHASHSEED=0 PYTHONPATH=/app:/base python /build_tpch_sql.py /app/tpch_sql.json
COPY tools/build_replays.py /build_replays.py
RUN PYTHONHASHSEED=0 PYTHONPATH=/app:/base python /build_replays.py /app/replay_index.z /app/optimizer_inputs
COPY requirements.lock /app/requirements.lock
RUN python -m pip install --disable-pip-version-check --no-cache-dir --no-deps --index-url https://pypi.org/simple --only-binary=:all: -r /app/requirements.lock
ENV SKIP_INTEGRATION=0 OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 PYTHON_CPU_COUNT=2
COPY query_sentinel.py ./
RUN PYTHONHASHSEED=0 PYTHONPATH=/app:/base python query_sentinel.py
COPY decision.py selector.py narrow.py fixture_reads.json long_gate.py ./
COPY incumbent/ ./incumbent/
RUN ln -s ../replay_index.z incumbent/replay_index.z \
    && ln -s ../optimizer_inputs incumbent/optimizer_inputs
CMD ["python", "/app/selector.py"]
