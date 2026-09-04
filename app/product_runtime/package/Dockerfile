FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /srv/product
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && useradd --uid 10001 --create-home product
COPY --chown=product:product runtime ./runtime
COPY --chown=product:product model.json manifest.json ./
USER product
EXPOSE 8000
CMD ["uvicorn", "runtime.server:from_environment", "--factory", "--host", "0.0.0.0", "--port", "8000", "--no-proxy-headers", "--no-access-log", "--limit-concurrency", "64", "--timeout-keep-alive", "5"]
