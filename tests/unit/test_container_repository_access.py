"""Checkout credentials belong to the container and outlive active workflows."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

import app.api.container as composition
from app.infrastructure.scm import GitHubAppTokenProvider, StaticTokenProvider
from app.infrastructure.settings import Settings
from app.providers.registry import ProviderRouter


def compose(monkeypatch, tmp_path, *, enabled, provider):
    router = Mock(spec=ProviderRouter)
    router.escalate.side_effect = lambda tier: tier
    monkeypatch.setattr(
        composition, "build_provider_router", lambda *args, **kwargs: router
    )
    graph = {}
    monkeypatch.setattr(
        composition, "build_workflow", lambda **kwargs: graph.update(kwargs)
    )
    provider_factory = Mock(return_value=provider)
    monkeypatch.setattr(composition, "build_token_provider_from_env", provider_factory)
    access_factory = Mock(wraps=composition.GitHubRepositoryAccess)
    monkeypatch.setattr(composition, "GitHubRepositoryAccess", access_factory)
    managers = []
    manager_class = composition.LocalGitWorkspaceManager

    def manager(*args, **kwargs):
        managers.append(kwargs)
        return manager_class(*args, **kwargs)

    monkeypatch.setattr(composition, "LocalGitWorkspaceManager", manager)
    settings = Settings(
        _env_file=None,
        factory_mode_enabled=enabled,
        factory_workspace_root=str(tmp_path / "factory"),
        executor_workspace_root=str(tmp_path / "executor"),
        repository_root=str(tmp_path),
        agent_tools_enabled=False,
    )
    container = composition.build_container(
        settings, None, None, False, factory_build_runner=object()
    )
    return container, graph, provider_factory, access_factory, managers


@pytest.mark.asyncio
async def test_disabled_factory_never_reads_checkout_credentials(monkeypatch, tmp_path):
    container, graph, provider_factory, access_factory, managers = compose(
        monkeypatch,
        tmp_path,
        enabled=False,
        provider=None,
    )
    assert graph["workspace_manager"] is None and managers == []
    provider_factory.assert_not_called()
    access_factory.assert_not_called()
    await container.shutdown()


@pytest.mark.skipif(os.name != "posix", reason="factory workspace requires POSIX")
@pytest.mark.asyncio
async def test_factory_without_credentials_preserves_anonymous_checkout(
    monkeypatch, tmp_path
):
    container, graph, provider_factory, access_factory, managers = compose(
        monkeypatch,
        tmp_path,
        enabled=True,
        provider=None,
    )
    provider_factory.assert_called_once_with()
    access_factory.assert_not_called()
    assert len(managers) == 1 and managers[0]["repository_access"] is None
    assert graph["workspace_manager"] is not None
    await container.shutdown()


@pytest.mark.skipif(os.name != "posix", reason="factory workspace requires POSIX")
@pytest.mark.asyncio
async def test_factory_wires_one_provider_without_resolving_token(
    monkeypatch, tmp_path
):
    provider = StaticTokenProvider("test-checkout-token")
    token = AsyncMock(side_effect=AssertionError("composition must not resolve tokens"))
    monkeypatch.setattr(provider, "token", token)
    container, graph, provider_factory, access_factory, managers = compose(
        monkeypatch,
        tmp_path,
        enabled=True,
        provider=provider,
    )
    provider_factory.assert_called_once_with()
    access_factory.assert_called_once_with(provider)
    assert len(managers) == 1 and managers[0]["repository_access"] is not None
    assert graph["workspace_manager"] is not None
    token.assert_not_awaited()
    await container.shutdown()
    await container.shutdown()
    provider_factory.assert_called_once()


@pytest.mark.parametrize("failure", [None, RuntimeError, asyncio.CancelledError])
@pytest.mark.asyncio
async def test_shutdown_stops_service_before_closing_owned_app_provider(failure):
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: pytest.fail("network call"))
    )
    provider = GitHubAppTokenProvider(
        "app-fixture", "installation-fixture", "not-a-real-key", client=client
    )
    calls = []

    async def stop_service():
        assert not client.is_closed
        calls.append("service")
        if failure:
            raise failure()

    async def close_provider():
        calls.append("provider")
        await client.aclose()

    service = SimpleNamespace(shutdown=AsyncMock(side_effect=stop_service))
    provider.close = AsyncMock(side_effect=close_provider)
    container = composition.Container(
        service, None, None, repository_token_provider=provider
    )
    if failure:
        with pytest.raises(failure):
            await container.shutdown()
    else:
        await asyncio.gather(container.shutdown(), container.shutdown())
    await container.shutdown()
    assert calls == ["service", "provider"] and client.is_closed
    service.shutdown.assert_awaited_once()
    provider.close.assert_awaited_once()


@pytest.mark.skipif(os.name != "posix", reason="factory workspace requires POSIX")
@pytest.mark.asyncio
async def test_composed_container_closes_app_provider_on_shutdown(
    monkeypatch, tmp_path
):
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: pytest.fail("network call"))
    )
    provider = GitHubAppTokenProvider(
        "app-fixture", "installation-fixture", "not-a-real-key", client=client
    )
    container, _, provider_factory, _, _ = compose(
        monkeypatch,
        tmp_path,
        enabled=True,
        provider=provider,
    )
    assert not client.is_closed
    await container.shutdown()
    assert client.is_closed
    provider_factory.assert_called_once_with()


def lifecycle_resources(monkeypatch, module, container):
    @asynccontextmanager
    async def resource(settings):
        yield object()

    for name in (
        "checkpointer_context",
        "workflow_queue_context",
        "project_memory_context",
        "tracing_context",
    ):
        monkeypatch.setattr(module, name, resource)
    monkeypatch.setattr(module, "build_container", Mock(return_value=container))


@pytest.mark.asyncio
async def test_api_lifespan_releases_container_even_when_app_fails(monkeypatch):
    import app.main as server

    service = SimpleNamespace(start_workers=Mock(), shutdown=AsyncMock())
    container = SimpleNamespace(workflow_service=service, shutdown=AsyncMock())
    lifecycle_resources(monkeypatch, server, container)
    app = SimpleNamespace(
        state=SimpleNamespace(
            settings=SimpleNamespace(run_embedded_workflow_workers=True)
        )
    )
    with pytest.raises(RuntimeError, match="application failed"):
        async with server.lifespan(app):
            assert app.state.container is container
            raise RuntimeError("application failed")
    service.start_workers.assert_called_once_with()
    service.shutdown.assert_not_awaited()
    container.shutdown.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_dedicated_worker_releases_container_after_stop(monkeypatch):
    import app.worker as worker

    service = SimpleNamespace(start_workers=Mock(), shutdown=AsyncMock())
    container = SimpleNamespace(workflow_service=service, shutdown=AsyncMock())
    lifecycle_resources(monkeypatch, worker, container)
    monkeypatch.setattr(worker, "get_settings", lambda: object())
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "add_signal_handler", lambda signal, callback: callback())
    await worker.run_worker()
    service.start_workers.assert_called_once_with()
    service.shutdown.assert_not_awaited()
    container.shutdown.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_api_lifespan_closes_container_when_starting_workers_fails(monkeypatch):
    import app.main as server

    service = SimpleNamespace(
        start_workers=Mock(side_effect=RuntimeError("startup failed"))
    )
    container = SimpleNamespace(workflow_service=service, shutdown=AsyncMock())
    lifecycle_resources(monkeypatch, server, container)
    app = SimpleNamespace(
        state=SimpleNamespace(
            settings=SimpleNamespace(run_embedded_workflow_workers=True)
        )
    )
    with pytest.raises(RuntimeError, match="startup failed"):
        async with server.lifespan(app):
            pytest.fail("lifespan must not yield after failed startup")
    container.shutdown.assert_awaited_once_with()
