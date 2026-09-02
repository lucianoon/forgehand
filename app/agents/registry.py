"""Registro de executores — seleção por capacidade (protocolo ExecutorRegistry).

Mantém o AgentProfile do desenho original, agora amarrado a ModelTier em vez
de string de modelo: o perfil declara "quanto raciocínio preciso", o
ProviderRouter decide qual modelo entrega isso (regra 1 + regra 9).
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.agents.executor import ExecutionStrategy, LLMExecutor, WorkspaceRuntime
from app.agents.tools import AgentTool
from app.models.task import AgentTask, Capability
from app.providers.registry import ModelTier, ProviderRouter


class AgentProfile(BaseModel):
    name: str
    capabilities: set[Capability]
    tier: ModelTier = ModelTier.STANDARD
    cost_level: int = Field(ge=1, le=5, default=2)
    max_parallel_tasks: int = Field(default=3, ge=1)


DEFAULT_PROFILES = [
    AgentProfile(
        name="backend_executor",
        capabilities={Capability.BACKEND, Capability.DEVOPS},
        tier=ModelTier.STANDARD,
        cost_level=2,
    ),
    AgentProfile(
        name="quality_executor",
        capabilities={Capability.TESTING, Capability.REVIEW, Capability.SECURITY},
        tier=ModelTier.STANDARD,
        cost_level=2,
    ),
    AgentProfile(
        name="docs_executor",
        capabilities={Capability.DOCUMENTATION, Capability.RESEARCH},
        tier=ModelTier.FAST,
        cost_level=1,
    ),
    AgentProfile(
        name="architecture_executor",
        capabilities={Capability.ARCHITECTURE, Capability.FRONTEND},
        tier=ModelTier.STANDARD,
        cost_level=3,
    ),
]


class CapabilityExecutorRegistry:
    """Implementa o protocolo ExecutorRegistry de app.graph.nodes.

    Seleção: entre os perfis que cobrem a capability, vence o de menor
    cost_level — o mais barato que resolve. Escalonamento de tier é
    responsabilidade do advisor, nunca da seleção inicial.
    """

    def __init__(
        self,
        router: ProviderRouter,
        profiles: list[AgentProfile] | None = None,
        workspace_runtime: WorkspaceRuntime | None = None,
        max_autocorrect_rounds: int = 0,
        execution_strategies: dict[Capability, ExecutionStrategy] | None = None,
        tools: list[AgentTool] | None = None,
        max_tool_calls: int = 8,
    ):
        self._router = router
        self._profiles = sorted(
            profiles or DEFAULT_PROFILES, key=lambda p: p.cost_level
        )
        covered = set().union(*(p.capabilities for p in self._profiles))
        missing = set(Capability) - covered
        if missing:
            raise ValueError(
                f"Nenhum perfil cobre as capabilities: {sorted(m.value for m in missing)}"
            )
        self._executors: dict[str, LLMExecutor] = {
            p.name: LLMExecutor(
                router,
                agent_name=p.name,
                tier=p.tier,
                workspace_runtime=workspace_runtime,
                max_autocorrect_rounds=max_autocorrect_rounds,
                execution_strategies=execution_strategies,
                tools=tools,
                max_tool_calls=max_tool_calls,
            )
            for p in self._profiles
        }
        # Um tier acima do perfil — usado só quando o advisor concedeu
        # task.tier_escalated (regra 8: modelo caro entra por escalonamento).
        self._escalated_executors: dict[str, LLMExecutor] = {
            p.name: LLMExecutor(
                router,
                agent_name=p.name,
                tier=router.escalate(p.tier),
                workspace_runtime=workspace_runtime,
                max_autocorrect_rounds=max_autocorrect_rounds,
                execution_strategies=execution_strategies,
                tools=tools,
                max_tool_calls=max_tool_calls,
            )
            for p in self._profiles
        }

    def select(self, task: AgentTask) -> LLMExecutor:
        profile = self._profile_for(task)
        if task.tier_escalated:
            return self._escalated_executors[profile.name]
        return self._executors[profile.name]

    def dispatch_policy(self, task: AgentTask) -> tuple[str, int]:
        profile = self._profile_for(task)
        return profile.name, profile.max_parallel_tasks

    def _profile_for(self, task: AgentTask) -> AgentProfile:
        for profile in self._profiles:  # já ordenado por custo
            if task.capability in profile.capabilities:
                return profile
        raise LookupError(
            f"Sem executor para {task.capability}"
        )  # inalcançável pós-__init__
