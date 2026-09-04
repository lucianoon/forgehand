"""Composição de dependências (composition root).

Aqui — e SÓ aqui — as camadas se conhecem: agentes recebem o ProviderRouter,
o grafo recebe agentes e checkpointer, o Container amarra serviço, fila e
auditoria. anthropic_client/openrouter_client injetáveis: testes passam
transporte mockado sem tocar em variáveis de ambiente.
"""

from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncGenerator
from typing import Any

import anthropic

from app.agents.advisor import LLMAdvisor
from app.agents.hooks import ToolHookDispatcher
from app.agents.executor import ExecutionStrategy
from app.agents.judge import LLMJudge
from app.agents.planner import LLMPlanner
from app.agents.product import ProductStudio
from app.infrastructure.product_store import ProductStore
from app.agents.registry import CapabilityExecutorRegistry
from app.agents.tools import AgentTool, build_workspace_tools
from app.agents.web_tools import FetchUrlTool
from app.agents.validation import ObjectiveValidationPipeline
from app.api.service import WorkflowService
from app.factory.build_strategy import BuildProfileRegistry
from app.factory.sandbox import DockerBuildRunner, DockerCLI
from app.factory.workspace import LocalGitWorkspaceManager
from app.graph.workflow import build_serde, build_workflow
from app.infrastructure.audit import InMemoryAuditLog, JsonlAuditLog, build_audit_event
from app.infrastructure.memory import InMemoryProjectMemory
from app.infrastructure.repository_grounding import RepositoryGroundingCollector
from app.infrastructure.web_references import WebReferenceCollector
from app.infrastructure.scm import GitHubDeliveryService
from app.infrastructure.settings import Settings
from app.infrastructure.workspace_runtime import (
    CommandObjectiveValidator,
    DockerSandboxCommandRunner,
    LocalCommandRunner,
    LocalWorkspaceRuntime,
)
from app.infrastructure.webhooks import WebhookDispatcher
from app.models.factory import BuildProfileSelection, WorkspaceLease
from app.models.build_execution import BuildRunResult
from app.models.task import Capability, TaskBudget
from app.providers.anthropic_provider import AnthropicProvider
from app.providers.base import LLMProvider
from app.providers.openai_compatible import OpenAICompatibleProvider
from app.providers.registry import ProviderRouter


@asynccontextmanager
async def checkpointer_context(settings: Settings) -> AsyncGenerator[Any, None]:
    """O checkpointer Postgres é um recurso com lifecycle (pool de conexões
    + setup de tabelas) — por isso context manager, não factory. O lifespan
    do FastAPI segura o contexto pela vida do processo."""
    if settings.checkpointer_backend == "postgres":
        # import tardio: dependência opcional (extra [postgres])
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

        async with AsyncPostgresSaver.from_conn_string(
            settings.database_url, serde=build_serde()
        ) as saver:
            await saver.setup()  # idempotente: cria tabelas se não existem
            yield saver
    else:
        from langgraph.checkpoint.memory import MemorySaver

        yield MemorySaver(serde=build_serde())


class Container:
    def __init__(self, service: WorkflowService, job_queue: Any, audit_log: Any,
                 product_studio: ProductStudio | None = None):
        self.workflow_service = service
        self.job_queue = job_queue
        self.audit_log = audit_log
        self.product_studio = product_studio


class LeaseBoundRuntimeFactory:
    """Constrói agentes cujas ferramentas apontam somente para uma lease."""

    def __init__(
        self,
        settings: Settings,
        router: ProviderRouter,
        hooks: ToolHookDispatcher | None = None,
    ) -> None:
        self._settings = settings
        self._router = router
        self._hooks = hooks
        self._components: dict[
            str, tuple[LLMPlanner, CapabilityExecutorRegistry, LLMJudge]
        ] = {}

    def _bound_settings(self, lease: WorkspaceLease) -> Settings:
        return self._settings.model_copy(
            update={
                "repository_root": lease.local_path,
                "executor_workspace_root": lease.local_path,
                "executor_apply_files_enabled": True,
                "executor_command_backend": self._settings.factory_command_backend,
                "executor_sandbox_image": self._settings.factory_sandbox_image,
                "executor_sandbox_network_enabled": (
                    self._settings.factory_sandbox_network_enabled
                ),
                # O pipeline legado aceita comandos string globais. No modo
                # fábrica, só as fases tipadas do perfil poderão executar;
                # até o phase runner ser conectado, não há fallback shell.
                "pytest_validation_command": None,
                "ruff_validation_command": None,
                "mypy_validation_command": None,
                "agent_tools_allow_checks": False,
            }
        )

    def _for_lease(
        self, lease: WorkspaceLease
    ) -> tuple[LLMPlanner, CapabilityExecutorRegistry, LLMJudge]:
        cached = self._components.get(lease.local_path)
        if cached is not None:
            return cached
        settings = self._bound_settings(lease)
        validators = build_objective_validators(settings)
        pipeline = build_objective_validation_pipeline(settings, validators)
        strategies = build_execution_strategies(settings)
        planner = LLMPlanner(
            self._router,
            default_task_budget=TaskBudget(
                max_tokens=settings.default_task_max_tokens,
                max_cost_usd=settings.default_task_max_cost_usd,
            ),
            tools=build_agent_tools(settings, lease.local_path, role="planner"),
            max_tool_calls=settings.agent_tools_max_calls_planner,
            hooks=self._hooks,
            non_writing_capabilities={
                capability
                for capability, strategy in strategies.items()
                if not strategy.apply_files
            },
            apply_files_enabled=True,
            require_write_paths=True,
        )
        registry = CapabilityExecutorRegistry(
            self._router,
            workspace_runtime=build_workspace_runtime(settings, pipeline),
            max_autocorrect_rounds=settings.executor_max_autocorrect_rounds,
            execution_strategies=strategies,
            tools=build_agent_tools(
                settings, lease.local_path, validators=validators, role="executor"
            ),
            max_tool_calls=settings.agent_tools_max_calls_executor,
            hooks=self._hooks,
        )
        judge = LLMJudge(
            self._router,
            validation_pipeline=pipeline,
            tools=build_agent_tools(settings, lease.local_path, role="judge"),
            max_tool_calls=settings.agent_tools_max_calls_judge,
            hooks=self._hooks,
            independence=settings.judge_independence,
            critical_quorum=settings.judge_critical_quorum,
        )
        components = (planner, registry, judge)
        self._components[lease.local_path] = components
        return components

    def build_grounding(self, lease: WorkspaceLease, request: str) -> dict[str, Any]:
        settings = self._bound_settings(lease)
        return RepositoryGroundingCollector(
            lease.local_path,
            max_files=settings.repository_grounding_max_files,
            max_excerpt_lines=settings.repository_grounding_max_lines_per_file,
            max_file_bytes=settings.repository_grounding_max_file_bytes,
            full_file_max_bytes=settings.repository_grounding_full_file_max_bytes,
        ).collect(request)

    def build_planner(self, lease: WorkspaceLease) -> LLMPlanner:
        return self._for_lease(lease)[0]

    def build_registry(self, lease: WorkspaceLease) -> CapabilityExecutorRegistry:
        return self._for_lease(lease)[1]

    def build_judge(self, lease: WorkspaceLease) -> LLMJudge:
        return self._for_lease(lease)[2]


def build_container(
    settings: Settings,
    checkpointer: Any,
    job_queue: Any,
    run_workers: bool,
    anthropic_client: anthropic.AsyncAnthropic | None = None,
    openrouter_client: Any | None = None,
    audit_log: Any | None = None,
    memory: Any | None = None,
    tracer: Any | None = None,
    factory_build_runner: Any | None = None,
    openai_client: Any | None = None,
) -> Container:
    """Checkpointer, memória e tracer vêm de fora (checkpointer_context,
    project_memory_context e tracing_context no lifespan) porque têm
    lifecycle próprio. anthropic_client injetável: testes passam transporte
    mockado sem tocar em variáveis de ambiente."""
    router = build_provider_router(
        settings,
        anthropic_client=anthropic_client,
        openrouter_client=openrouter_client,
        openai_client=openai_client,
        tracer=tracer,
    )
    objective_validators = build_objective_validators(settings)
    validation_pipeline = build_objective_validation_pipeline(
        settings,
        objective_validators,
    )
    execution_strategies = build_execution_strategies(settings)
    workspace_runtime = build_workspace_runtime(settings, validation_pipeline)
    selected_audit_log = audit_log or (
        JsonlAuditLog(settings.audit_log_path, max_events=settings.audit_log_max_events)
        if settings.audit_log_backend == "jsonl"
        else InMemoryAuditLog(max_events=settings.audit_log_max_events)
    )
    tool_hooks = (
        ToolHookDispatcher(
            settings.tool_hooks,
            selected_audit_log,
            timeout_seconds=settings.tool_hooks_timeout_seconds,
        )
        if settings.tool_hooks
        else None
    )

    async def record_strategy_selection(
        *,
        workflow_id: str,
        project_id: str,
        client_id: str,
        repository: str,
        selection: BuildProfileSelection,
    ) -> None:
        await selected_audit_log.record(
            build_audit_event(
                action="build_strategy_selection",
                outcome=selection.selection_reason or "invalid",
                client_id=client_id,
                project_id=project_id,
                workflow_id=workflow_id,
                detail=(
                    f"repository={repository};"
                    f"profile={selection.selected_profile or '-'};"
                    f"profile_digest={selection.profile_digest or '-'};"
                    f"reason={selection.unsupported_reason or '-'}"
                ),
            )
        )

    async def record_build_evidence(
        *,
        project_id: str,
        client_id: str,
        lease: WorkspaceLease,
        selection: BuildProfileSelection,
        report: BuildRunResult,
    ) -> None:
        phases = [
            {
                "name": phase.phase.value,
                "outcome": phase.outcome.value,
                "duration_seconds": round(phase.duration_seconds, 6),
                "exit_code": phase.exit_code,
                "error_code": phase.error_code,
                "output_truncated": phase.output_truncated,
            }
            for phase in report.phases
        ]
        await selected_audit_log.record(
            build_audit_event(
                action="factory_build_validation",
                outcome=report.outcome.value,
                client_id=client_id,
                project_id=project_id,
                workflow_id=lease.workflow_id,
                detail=json.dumps(
                    {
                        "profile": selection.selected_profile,
                        "profile_digest": selection.profile_digest,
                        "error_code": report.error_code,
                        "phases": phases,
                    },
                    separators=(",", ":"),
                ),
            )
        )

    workspace_manager = (
        LocalGitWorkspaceManager(
            settings.factory_workspace_root,
            approved_hosts=settings.factory_approved_scm_hosts,
        )
        if settings.factory_mode_enabled
        else None
    )
    runtime_factory = (
        LeaseBoundRuntimeFactory(settings, router, hooks=tool_hooks)
        if settings.factory_mode_enabled
        else None
    )
    build_strategy_selector = (
        BuildProfileRegistry(
            settings.factory_build_profiles,
            settings.factory_repository_profiles,
        )
        if settings.factory_mode_enabled
        else None
    )
    if settings.factory_mode_enabled and factory_build_runner is None:
        assert build_strategy_selector is not None
        redacted_values = tuple(
            value
            for name in (
                "ANTHROPIC_API_KEY",
                "OPENAI_API_KEY",
                "OPENROUTER_API_KEY",
                "GITHUB_TOKEN",
                "WEBHOOK_SIGNING_SECRET",
            )
            if (value := os.getenv(name))
        )
        factory_build_runner = DockerBuildRunner(
            build_strategy_selector,
            DockerCLI(socket_path=settings.factory_docker_socket),
            allow_dependency_network=settings.factory_sandbox_network_enabled,
            redacted_values=redacted_values,
            journal=workspace_manager.journal if workspace_manager else None,
        )
    # Executor e judge exploram o workspace onde os arquivos são aplicados;
    # o planner explora o repositório do grounding. run_check só no executor.
    ensure_executor_workspace(settings)
    executor_tools = build_agent_tools(
        settings,
        settings.executor_workspace_root,
        validators=objective_validators,
        role="executor",
    )
    judge_tools = build_agent_tools(
        settings, settings.executor_workspace_root, role="judge"
    )
    planner_tools = build_agent_tools(settings, settings.repository_root, role="planner")
    graph_app = build_workflow(
        planner=LLMPlanner(
            router,
            default_task_budget=TaskBudget(
                max_tokens=settings.default_task_max_tokens,
                max_cost_usd=settings.default_task_max_cost_usd,
            ),
            tools=planner_tools,
            max_tool_calls=settings.agent_tools_max_calls_planner,
            hooks=tool_hooks,
            non_writing_capabilities={
                capability
                for capability, strategy in execution_strategies.items()
                if not strategy.apply_files
            },
            apply_files_enabled=workspace_runtime is not None,
        ),
        registry=CapabilityExecutorRegistry(
            router,
            workspace_runtime=workspace_runtime,
            max_autocorrect_rounds=settings.executor_max_autocorrect_rounds,
            execution_strategies=execution_strategies,
            tools=executor_tools,
            max_tool_calls=settings.agent_tools_max_calls_executor,
            hooks=tool_hooks,
        ),
        judge=LLMJudge(
            router,
            validation_pipeline=validation_pipeline,
            tools=judge_tools,
            max_tool_calls=settings.agent_tools_max_calls_judge,
            hooks=tool_hooks,
            independence=settings.judge_independence,
            critical_quorum=settings.judge_critical_quorum,
        ),
        memory=memory or InMemoryProjectMemory(settings),
        checkpointer=checkpointer,
        advisor=LLMAdvisor(router),
        delivery=GitHubDeliveryService(
            poll_interval_seconds=settings.delivery_checks_poll_interval_seconds,
            grace_seconds=settings.delivery_checks_grace_seconds,
        ),
        workspace_manager=workspace_manager,
        runtime_factory=runtime_factory,
        build_strategy_selector=build_strategy_selector,
        strategy_audit_recorder=(
            record_strategy_selection if settings.factory_mode_enabled else None
        ),
        build_runner=factory_build_runner,
        build_audit_recorder=(
            record_build_evidence if settings.factory_mode_enabled else None
        ),
    )
    event_publisher = WebhookDispatcher(
        settings.webhook_urls,
        os.getenv("WEBHOOK_SIGNING_SECRET", ""),
    )
    return Container(
        WorkflowService(
            graph_app,
            settings,
            job_queue,
            run_workers,
            event_publisher,
            tracer=tracer,
            workspace_manager=workspace_manager,
            build_runner=factory_build_runner,
            audit_log=selected_audit_log,
        ),
        job_queue,
        selected_audit_log,
        ProductStudio(router, ProductStore(settings.product_studio_database))
        if settings.product_studio_enabled else None,
    )


def ensure_executor_workspace(settings: Settings) -> Path:
    """Garante o diretório que executor e judge exploram (e onde o executor
    escreve, se habilitado). Sem isso, list_directory no default falharia."""
    root = Path(settings.executor_workspace_root).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    return root.resolve()


def build_agent_tools(
    settings: Settings,
    root: str,
    *,
    validators: list[CommandObjectiveValidator] | None = None,
    role: str = "executor",
) -> list[AgentTool]:
    if not settings.agent_tools_enabled:
        return []
    tools = build_workspace_tools(
        root,
        max_output_chars=settings.agent_tools_max_output_chars,
        validators=list(validators)
        if validators and settings.agent_tools_allow_checks
        else None,
    )
    if settings.agent_web_fetch_enabled and role in web_fetch_roles(settings):
        tools.append(
            FetchUrlTool(
                WebReferenceCollector.from_settings(settings),
                max_output_chars=settings.agent_tools_max_output_chars,
            )
        )
    return tools


def web_fetch_roles(settings: Settings) -> set[str]:
    return {role.strip() for role in settings.agent_web_fetch_roles.split(",") if role.strip()}


def build_workspace_runtime(
    settings: Settings,
    validation_pipeline: ObjectiveValidationPipeline,
) -> LocalWorkspaceRuntime | None:
    if not settings.executor_apply_files_enabled:
        return None
    return LocalWorkspaceRuntime(
        settings.executor_workspace_root,
        apply_files_enabled=settings.executor_apply_files_enabled,
        validation_pipeline=validation_pipeline,
    )


def build_objective_validators(settings: Settings) -> list[CommandObjectiveValidator]:
    validators: list[CommandObjectiveValidator] = []
    command_runner = (
        DockerSandboxCommandRunner(
            image=settings.executor_sandbox_image,
            memory=settings.executor_sandbox_memory,
            cpus=settings.executor_sandbox_cpus,
            network_enabled=settings.executor_sandbox_network_enabled,
        )
        if settings.executor_command_backend == "docker"
        else LocalCommandRunner()
    )
    if settings.pytest_validation_command:
        validators.append(
            CommandObjectiveValidator(
                name="pytest",
                command=settings.pytest_validation_command,
                workspace_root=settings.executor_workspace_root,
                capabilities={
                    Capability.BACKEND,
                    Capability.FRONTEND,
                    Capability.TESTING,
                    Capability.SECURITY,
                },
                command_runner=command_runner,
            )
        )
    if settings.ruff_validation_command:
        validators.append(
            CommandObjectiveValidator(
                name="ruff",
                command=settings.ruff_validation_command,
                workspace_root=settings.executor_workspace_root,
                file_suffixes={".py"},
                command_runner=command_runner,
            )
        )
    if settings.mypy_validation_command:
        validators.append(
            CommandObjectiveValidator(
                name="mypy",
                command=settings.mypy_validation_command,
                workspace_root=settings.executor_workspace_root,
                file_suffixes={".py"},
                capabilities={
                    Capability.BACKEND,
                    Capability.ARCHITECTURE,
                    Capability.TESTING,
                },
                command_runner=command_runner,
            )
        )
    return validators


def build_objective_validation_pipeline(
    settings: Settings,
    validators: list[CommandObjectiveValidator],
) -> ObjectiveValidationPipeline:
    capability_pipelines: dict[Capability, list[str]] = {}
    for (
        raw_capability,
        validator_names,
    ) in settings.objective_validation_pipelines.items():
        try:
            capability = Capability(raw_capability)
        except ValueError:
            continue
        capability_pipelines[capability] = validator_names
    return ObjectiveValidationPipeline(
        validators,
        capability_pipelines=capability_pipelines,
    )


def build_execution_strategies(
    settings: Settings,
) -> dict[Capability, ExecutionStrategy]:
    strategies: dict[Capability, ExecutionStrategy] = {}
    for raw_capability, strategy in settings.executor_strategies.items():
        try:
            capability = Capability(raw_capability)
        except ValueError:
            continue
        strategies[capability] = strategy
    return strategies


def build_provider_router(
    settings: Settings,
    anthropic_client: anthropic.AsyncAnthropic | None = None,
    openrouter_client: Any | None = None,
    tracer: Any | None = None,
    openai_client: Any | None = None,
) -> ProviderRouter:
    provider: LLMProvider
    providers: dict[str, LLMProvider]
    if settings.llm_provider_backend == "openai":
        # Never borrow credentials or endpoint settings from another provider.
        api_key = os.getenv("OPENAI_API_KEY", "").strip()
        if not api_key and openai_client is None:
            raise ValueError("OPENAI_API_KEY is required for the OpenAI backend")
        provider = OpenAICompatibleProvider(
            settings.pricing,
            base_url="https://api.openai.com",
            api_key=api_key,
            provider_name="openai",
            supports_json_schema=True,
            supports_prompt_caching=False,  # OpenAI caches prefixes automatically.
            client=openai_client,
        )
        providers = {"openai": provider}
    elif settings.llm_provider_backend == "openrouter":
        openrouter_api_key = os.getenv("OPENROUTER_API_KEY") or os.getenv(
            "ANTHROPIC_API_KEY"
        )
        provider = OpenAICompatibleProvider(
            settings.pricing,
            base_url=settings.openrouter_base_url,
            api_key=openrouter_api_key,
            provider_name="openrouter",
            supports_json_schema=settings.openrouter_supports_json_schema,
            require_parameters=settings.openrouter_require_parameters,
            response_healing=settings.openrouter_response_healing,
            supports_prompt_caching=settings.openrouter_prompt_caching,
            extra_headers={
                **(
                    {"HTTP-Referer": settings.openrouter_http_referer}
                    if settings.openrouter_http_referer
                    else {}
                ),
                **(
                    {"X-Title": settings.openrouter_app_name}
                    if settings.openrouter_app_name
                    else {}
                ),
            },
            client=openrouter_client,
        )
        providers = {"openrouter": provider}
    else:
        provider = AnthropicProvider(settings.pricing, client=anthropic_client)
        providers = {"anthropic": provider}
    judge_bindings = settings.judge_tier_bindings
    return ProviderRouter(
        providers=providers,
        bindings=settings.tier_bindings,
        tracer=tracer,
        role_bindings={"judge": judge_bindings} if judge_bindings else None,
    )
