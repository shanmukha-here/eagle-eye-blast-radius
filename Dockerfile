FROM alpine/git:v2.49.1@sha256:c0280cf9572316299b08544065d3bf35db65043d5e3963982ec50647d2746e26 AS source
RUN git init -q /src && git -C /src fetch --depth=1 --no-tags https://github.com/tobymao/sqlglot.git eb11aec4be8cb2122afe713a4816662c3f0fb2fa \
    && git -C /src checkout -q --detach FETCH_HEAD \
    && test "$(git -C /src rev-parse HEAD)" = eb11aec4be8cb2122afe713a4816662c3f0fb2fa \
    && rm -rf /src/.git

FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
ENV PYTHONDONTWRITEBYTECODE=1
COPY --from=source /src/sqlglot /base/sqlglot
COPY --from=source /src/tests /base/tests
RUN python -m compileall -q /base/sqlglot
WORKDIR /app
COPY selector.py impact.py refine.py import_check.py dependencies.py base.z details.z access.z ./
CMD ["python", "/app/selector.py"]
