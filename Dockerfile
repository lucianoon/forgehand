# syntax=docker/dockerfile:1

ARG PYTHON_IMAGE=python:3.12-slim-bookworm
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.11.23
ARG DOCKER_CLI_IMAGE=docker:29.4.0-cli

FROM ${UV_IMAGE} AS uv
FROM ${DOCKER_CLI_IMAGE} AS docker-cli

FROM ${PYTHON_IMAGE} AS builder
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /build
COPY pyproject.toml uv.lock ./
# Resolve from the committed lock, retaining wheel hashes. The project itself
# has no build-system entry, so install its CLI separately without dependencies.
RUN uv export --frozen --no-dev --extra postgres --extra github-app \
      --no-emit-project --format requirements-txt --output-file requirements.txt \
    && uv pip install --system --no-cache --require-hashes -r requirements.txt
COPY app ./app
RUN uv pip install --system --no-cache --no-deps .

FROM builder AS test
RUN uv export --frozen --no-dev --extra dev --extra postgres --extra github-app \
      --no-emit-project --format requirements-txt --output-file requirements-test.txt \
    && uv pip install --system --no-cache --require-hashes -r requirements-test.txt
COPY tests ./tests
CMD ["pytest", "-q"]

FROM ${PYTHON_IMAGE} AS runtime
# PGDG supplies client 16 on both amd64 and arm64; no PostgreSQL server or Docker
# daemon is installed in the application image.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && install -d /usr/share/postgresql-common/pgdg \
    && curl --fail --silent --show-error --location \
      https://www.postgresql.org/media/keys/ACCC4CF8.asc \
      --output /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
    && echo 'deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt bookworm-pgdg main' \
      > /etc/apt/sources.list.d/pgdg.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends git postgresql-client-16 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 1000 --create-home --shell /usr/sbin/nologin forge

COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker
COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin
WORKDIR /srv/forgehand
COPY app ./app

ARG FORGEHAND_REVISION=development
ENV FORGEHAND_REVISION=${FORGEHAND_REVISION} \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/usr/lib/postgresql/16/bin:${PATH}
LABEL org.opencontainers.image.revision=${FORGEHAND_REVISION}
USER 1000:1000
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import httpx; httpx.get('http://127.0.0.1:8000/health', timeout=2).raise_for_status()"
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
