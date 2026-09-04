from unittest.mock import AsyncMock

import httpx
import pytest

from app.api.service import WorkflowService
from app.infrastructure.settings import Settings
from app.infrastructure.workflow_queue import InMemoryWorkflowQueue
from app.providers.base import RetryableProviderError


@pytest.mark.asyncio
@pytest.mark.parametrize("persistence_fails", [False, True])
@pytest.mark.parametrize("http_status,expected", [(429, "HTTP429"), (None, "ReadTimeout")])
async def test_failure_diagnostics_persist_safe_reason_without_response_body(
    caplog, http_status, expected, persistence_fails
):
    graph = AsyncMock()
    if persistence_fails:
        graph.aupdate_state.side_effect = RuntimeError("checkpoint unavailable")
    service = WorkflowService(graph, Settings(), InMemoryWorkflowQueue(), False)
    secret = "sk-test-sensitive-payload"
    error = RetryableProviderError(secret, "openai", http_status)
    error.__cause__ = httpx.ReadTimeout(secret)
    try:
        raise error
    except RetryableProviderError as caught:
        await service._mark_failed("workflow-test", caught)
    values = graph.aupdate_state.call_args.args[1]
    assert values["error"] == f"RetryableProviderError:{expected}"
    assert secret not in str(values)
    assert secret not in caplog.text
