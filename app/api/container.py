"""Composição de dependências (composition root).

Aqui — e SÓ aqui — as camadas se conhecem: agentes recebem o ProviderRouter,
o grafo recebe agentes e checkpointer, o Container amarra serviço, fila e
auditoria. anthropic_client/openrouter_client injetáveis: testes passam
transporte mockado sem tocar em variáveis de ambiente.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import AsyncGenerator
from typing import Any

import anthropic

from app.agents.advisor import LLMAdvisor
from app.agents.executor import ExecutionStrategy
from app.agents.judge import LLMJudge
from app.agents.planner import LLMPlanner
from app.agents.registry import CapabilityExecutorRegistry
from app.agents.tools import AgentTool, build_workspace_tools
from app.agents.validation import ObjectiveValidationPipeline
from app.api.service import WorkflowService
from app.graph.workflow import build_serde, build_workflow
from app.infrastructure.audit import InMemoryAuditLog, JsonlAuditLog
from app.infrastructure.memory import InMemoryProjectMemory
from app.infrastructure.scm import GitHubDeliveryService
from app.infrastructure.settings import Settings
from app.infrastructure.workspace_runtime import (
    CommandObjectiveValidator,
    DockerSandboxCommandRunner,
    LocalCommandRunner,
    LocalWorkspaceRuntime,
)
from app.infrastructure.webhooks import WebhookDispatcher
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
    def __init__(self, service: WorkflowService, job_queue: Any, audit_log: Any):
        self.workflow_service = service
        self.job_queue = job_queue
        self.audit_log = audit_log


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
) -> Container:
    """Checkpointer, memória e tracer vêm de fora (checkpointer_context,
    project_memory_context e tracing_context no lifespan) porque têm
    lifecycle próprio. anthropic_client injetável: testes passam transporte
    mockado sem tocar em variáveis de ambiente."""
    router = build_provider_router(
        settings,
        anthropic_client=anthropic_client,
        openrouter_client=openrouter_client,
        tracer=tracer,
    )
    objective_validators = build_objective_validators(settings)
    validation_pipeline = build_objective_validation_pipeline(
        settings,
        objective_validators,
    )
    execution_strategies = build_execution_strategies(settings)
    workspace_runtime = build_workspace_runtime(settings, validation_pipeline)
    # Executor e judge exploram o workspace onde os arquivos são aplicados;
    # o planner explora o repositório do grounding. run_check só no executor.
    executor_tools = build_agent_tools(
        settings, settings.executor_workspace_root, validators=objective_validators
    )
    judge_tools = build_agent_tools(settings, settings.executor_workspace_root)
    planner_tools = build_agent_tools(settings, settings.repository_root)
    graph_app = build_workflow(
        planner=LLMPlanner(
            router,
            default_task_budget=TaskBudget(
                max_tokens=settings.default_task_max_tokens,
                max_cost_usd=settings.default_task_max_cost_usd,
            ),
            tools=planner_tools,
            max_tool_calls=settings.agent_tools_max_calls_planner,
        ),
        registry=CapabilityExecutorRegistry(
            router,
            workspace_runtime=workspace_runtime,
            max_autocorrect_rounds=settings.executor_max_autocorrect_rounds,
            execution_strategies=execution_strategies,
            tools=executor_tools,
            max_tool_calls=settings.agent_tools_max_calls_executor,
        ),
        judge=LLMJudge(
            router,
            validation_pipeline=validation_pipeline,
            tools=judge_tools,
            max_tool_calls=settings.agent_tools_max_calls_judge,
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
    )
    audit_log = audit_log or (
        JsonlAuditLog(settings.audit_log_path, max_events=settings.audit_log_max_events)
        if settings.audit_log_backend == "jsonl"
        else InMemoryAuditLog(max_events=settings.audit_log_max_events)
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
        ),
        job_queue,
        audit_log,
    )


def build_agent_tools(
    settings: Settings,
    root: str,
    *,
    validators: list[CommandObjectiveValidator] | None = None,
) -> list[AgentTool]:
    if not settings.agent_tools_enabled:
        return []
    return build_workspace_tools(
        root,
        max_output_chars=settings.agent_tools_max_output_chars,
        validators=list(validators)
        if validators and settings.agent_tools_allow_checks
        else None,
    )


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
) -> ProviderRouter:
    provider: LLMProvider
    providers: dict[str, LLMProvider]
    if settings.llm_provider_backend == "openrouter":
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
