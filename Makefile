PORT ?= 8000

.PHONY: demo
## Sobe o mission control local sem Postgres, Neo4j ou Docker.
demo:
	uv sync --extra dev --locked
	uv run uvicorn app.main:app --env-file .env.demo --port $(PORT)
