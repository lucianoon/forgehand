from __future__ import annotations

import asyncio
import signal

from app.api.container import build_container, checkpointer_context
from app.infrastructure.logging_setup import configure_logging
from app.infrastructure.memory import project_memory_context
from app.infrastructure.settings import get_settings
from app.infrastructure.tracing import tracing_context
from app.infrastructure.workflow_queue import workflow_queue_context


async def run_worker() -> None:
    settings = get_settings()
    async with checkpointer_context(settings) as checkpointer:
        async with workflow_queue_context(settings) as job_queue:
            async with project_memory_context(settings) as memory:
                async with tracing_context(settings) as tracer:
                    container = build_container(
                        settings,
                        checkpointer,
                        job_queue,
                        run_workers=True,
                        memory=memory,
                        tracer=tracer,
                    )
                    stop = asyncio.Event()

                    def _stop() -> None:
                        stop.set()

                    loop = asyncio.get_running_loop()
                    for sig in (signal.SIGINT, signal.SIGTERM):
                        try:
                            loop.add_signal_handler(sig, _stop)
                        except NotImplementedError:
                            pass

                    try:
                        container.workflow_service.start_workers()
                        await stop.wait()
                    finally:
                        await container.shutdown()


def main() -> None:
    configure_logging()
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
