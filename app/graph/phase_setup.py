"""Fase de preparação: workspace, estratégia de build, contexto e plano.

  provision_workspace → select_build_strategy → load_context → create_plan
"""

from __future__ import annotations

from typing import Any

from app.graph.contracts import NodeDependencies, PlanningOutcome
from app.graph.state import WorkflowPhase, WorkflowState
from app.models.factory import BuildProfileSelection, FactoryStage


def build_setup_nodes(deps: NodeDependencies) -> dict[str, Any]:
    async def provision_workspace(state: WorkflowState) -> dict[str, Any]:
        if state.work_order is None:
            return {"phase": WorkflowPhase.LOADING_CONTEXT}
        if deps.workspace_manager is None or deps.runtime_factory is None:
            return {
                "error": "factory runtime não configurado",
                "phase": WorkflowPhase.FAILED,
                "factory_stage": FactoryStage.PROVISIONING,
            }
        try:
            lease = (
                await deps.workspace_manager.reconstruct(state.workspace)
                if state.workspace is not None
                else await deps.workspace_manager.provision(
                    state.workflow_id, state.work_order
                )
            )
        except Exception as exc:  # noqa: BLE001
            return {
                "error": f"workspace provisioning failed: {type(exc).__name__}: {exc}",
                "phase": WorkflowPhase.FAILED,
                "factory_stage": FactoryStage.PROVISIONING,
            }
        return {
            "workspace": lease,
            "phase": WorkflowPhase.LOADING_CONTEXT,
            "factory_stage": FactoryStage.PROVISIONING,
        }

    def provision_router(state: WorkflowState) -> str:
        return (
            "persist_memory" if state.phase == WorkflowPhase.FAILED else "load_context"
        )

    async def select_build_strategy(state: WorkflowState) -> dict[str, Any]:
        """Persiste a política selecionada antes de ler ou executar o projeto."""
        if state.work_order is None:
            return {"phase": WorkflowPhase.LOADING_CONTEXT}
        if state.workspace is None:
            return {
                "error": "build strategy selection requires a workspace lease",
                "phase": WorkflowPhase.FAILED,
                "factory_stage": FactoryStage.STRATEGY_SELECTION,
            }

        current = state.build_strategy
        try:
            if deps.build_strategy_selector is None:
                raise ValueError("nenhum registro de perfis foi configurado.")
            if current is not None and current.selection_reason != "unsupported":
                # Retomadas só reutilizam a seleção se a definição administrada
                # ainda corresponder ao fingerprint persistido.
                deps.build_strategy_selector.profile_for(current)
                if current.acceptance_digest is not None and current.acceptance_criteria != state.work_order.acceptance_criteria:
                    raise ValueError("Critérios de aceitação mudaram após seleção.")
                selection = current
            else:
                selection = deps.build_strategy_selector.select(
                    state.work_order, state.workspace
                )
        except (TypeError, ValueError) as exc:
            selection = BuildProfileSelection(
                requested_profile=state.work_order.build_profile.requested_profile,
                selection_reason="unsupported",
                unsupported_reason=f"seleção persistida inválida: {exc}",
            )

        if deps.strategy_audit_recorder is not None:
            await deps.strategy_audit_recorder(
                workflow_id=state.workflow_id,
                project_id=state.project_id,
                client_id=state.owner_client_id,
                repository=state.work_order.repository.full_name,
                selection=selection,
            )

        unsupported = selection.selection_reason == "unsupported"
        return {
            "build_strategy": selection,
            "factory_stage": FactoryStage.STRATEGY_SELECTION,
            "phase": (
                WorkflowPhase.UNSUPPORTED_BUILD_STRATEGY
                if unsupported
                else WorkflowPhase.LOADING_CONTEXT
            ),
            "error": (
                f"unsupported_build_strategy: {selection.unsupported_reason}"
                if unsupported
                else None
            ),
        }

    def strategy_router(state: WorkflowState) -> str:
        if state.phase == WorkflowPhase.FAILED:
            return "persist_memory"
        if state.work_order is None:
            return "load_context"
        if (
            state.build_strategy is None
            or state.build_strategy.selection_reason == "unsupported"
        ):
            return "human_gate"
        return "load_context"

    async def load_context(state: WorkflowState) -> dict[str, Any]:
        memory = deps.memory
        if state.workspace is not None and deps.runtime_factory is not None:
            project_context_loader = getattr(memory, "load_project_context", None)
            ctx = (
                await project_context_loader(state.project_id, state.request)
                if callable(project_context_loader)
                else await memory.load_context(state.project_id, state.request)
            )
            ctx = dict(ctx)
            ctx.pop("repository_grounding", None)
            ctx["repository_grounding"] = deps.runtime_factory.build_grounding(
                state.workspace, state.request
            )
        else:
            ctx = await memory.load_context(state.project_id, state.request)
        ctx = dict(ctx)
        ctx.pop("architecture_policy_guidance", None)
        ctx.pop("acceptance_policy_guidance", None)
        if state.build_strategy is not None and deps.build_strategy_selector is not None:
            profile = deps.build_strategy_selector.profile_for(state.build_strategy)
            if profile.acceptance is not None:
                ctx["acceptance_policy_guidance"] = (
                    "Aceitação independente definida pelo operador; implemente o comportamento, não altere os critérios.\n"
                    + "\n".join(f"{case.id}: {case.criterion}" for case in profile.acceptance.cases)
                )
            if profile.architecture is not None:
                ctx["architecture_policy_guidance"] = (
                    "Política de arquitetura aprovada pelo operador. Não altere nem contorne as regras.\n"
                    + "\n".join(
                        f"{rule.id}: {rule.source} não pode importar {', '.join(rule.forbidden)}. {rule.remediation}"
                        for rule in profile.architecture.rules
                    )
                )
        return {"context": ctx, "phase": WorkflowPhase.PLANNING}

    async def create_plan(state: WorkflowState) -> dict[str, Any]:
        if state.plan:
            # Replays do nó de planning não podem chamar o planner novamente
            # nem anexar um segundo plano ao estado já persistido.
            return {
                "plan": state.plan,
                "usage": {"tokens": 0, "cost_usd": 0.0},
                "phase": WorkflowPhase.EXECUTING,
            }
        raw = await deps.active_planner(state).create_plan(state.request, state.context)
        if isinstance(raw, PlanningOutcome):
            plan = raw.plan
            usage = raw.usage.model_dump()
        else:  # compatibilidade com implementações do protocolo existentes
            plan = raw
            usage = {"tokens": 0, "cost_usd": 0.0}
        if not plan:
            raise ValueError("Planner retornou plano vazio.")
        update: dict[str, Any] = {
            "plan": plan,
            "usage": usage,
            "phase": WorkflowPhase.EXECUTING,
        }
        if state.work_order is not None:
            update["factory_stage"] = FactoryStage.IMPLEMENTATION
        return update

    return {
        "provision_workspace": provision_workspace,
        "provision_router": provision_router,
        "select_build_strategy": select_build_strategy,
        "strategy_router": strategy_router,
        "load_context": load_context,
        "create_plan": create_plan,
    }
