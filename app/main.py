"""Entrypoint. `uvicorn app.main:app --reload`"""

from __future__ import annotations

from contextlib import asynccontextmanager
from collections.abc import Awaitable, Callable
from pathlib import Path
import time
from typing import AsyncGenerator
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from app.api.container import build_container, checkpointer_context
from app.api.routes.operations import router as operations_router
from app.api.routes.workflows import router as workflows_router
from app.api.routes.products import router as products_router
from app.api.routes.product_deliveries import router as product_deliveries_router
from app.infrastructure.memory import project_memory_context
from app.infrastructure.settings import get_settings
from app.infrastructure.telemetry import HttpMetrics
from app.infrastructure.tracing import tracing_context
from app.infrastructure.workflow_queue import WorkflowDispatchConflict, workflow_queue_context

WEB_ROOT = Path(__file__).with_name("web")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    settings = app.state.settings
    async with checkpointer_context(settings) as checkpointer:
        async with workflow_queue_context(settings) as job_queue:
            async with project_memory_context(settings) as memory:
                async with tracing_context(settings) as tracer:
                    app.state.container = build_container(
                        settings,
                        checkpointer,
                        job_queue,
                        run_workers=settings.run_embedded_workflow_workers,
                        memory=memory,
                        tracer=tracer,
                    )
                    app.state.container.workflow_service.start_workers()
                    try:
                        yield
                    finally:
                        await app.state.container.workflow_service.shutdown()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="Forgehand", version="0.4.1", lifespan=lifespan)
    app.state.settings = settings
    app.state.http_metrics = HttpMetrics()

    @app.exception_handler(WorkflowDispatchConflict)
    async def dispatch_conflict(request: Request, exc: WorkflowDispatchConflict) -> JSONResponse:
        return JSONResponse(
            status_code=409,
            content={"detail": {"code": "workflow_dispatch_conflict", "message": str(exc)}},
        )

    @app.middleware("http")
    async def request_context(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request_id = str(uuid4())
        started = time.perf_counter()
        response = await call_next(request)
        elapsed = time.perf_counter() - started
        route = request.scope.get("route")
        route_path = getattr(route, "path", request.url.path)
        app.state.http_metrics.record(
            request.method, route_path, response.status_code, elapsed
        )
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Process-Time-Ms"] = f"{elapsed * 1000:.2f}"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    app.include_router(operations_router)
    app.include_router(workflows_router)
    app.include_router(products_router)
    app.include_router(product_deliveries_router)
    app.mount(
        "/dashboard/assets",
        StaticFiles(directory=WEB_ROOT),
        name="dashboard-assets",
    )

    @app.get("/", include_in_schema=False)
    async def home() -> RedirectResponse:
        return RedirectResponse("/dashboard")

    @app.get("/dashboard", include_in_schema=False)
    async def dashboard() -> FileResponse:
        return FileResponse(WEB_ROOT / "index.html")

    @app.get("/studio", include_in_schema=False)
    async def studio_page() -> FileResponse:
        return FileResponse(WEB_ROOT / "studio.html")

    return app


app = create_app()
