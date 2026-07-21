# syntax=docker/dockerfile:1

FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /build
COPY pyproject.toml ./
COPY app ./app

# Fase 1 já embarca o extra postgres: o checkpointer de produção é Postgres
RUN uv pip install --system --no-cache ".[postgres]"


FROM builder AS test

RUN uv pip install --system --no-cache ".[dev,postgres]"
COPY tests ./tests

CMD ["pytest", "-q"]


FROM python:3.12-slim

# Regra 7 em espírito: o serviço não roda como root
RUN useradd --create-home --shell /usr/sbin/nologin forge

COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

WORKDIR /srv/agent-forge
COPY app ./app

USER forge

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import httpx; httpx.get('http://127.0.0.1:8000/health', timeout=2).raise_for_status()"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
