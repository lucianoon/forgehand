import os
import pytest

from app.agents.planner import LLMPlanner, PlanOutput, PlanValidationError
from app.providers.base import CompletionResult, Usage


@pytest.mark.asyncio
async def test_planner_repairs_structurally_invalid_dependencies():
    class RepairingRouter:
        def __init__(self):
            self.requests = []

        async def complete(self, tier, request):
            self.requests.append(request)
            invalid = len(self.requests) == 1
            return CompletionResult(
                text="plan",
                parsed={
                    "rationale": "plano corrigido",
                    "tasks": [
                        {
                            "title": "Analisar",
                            "description": "Analisar arquitetura",
                            "capability": "architecture",
                            "acceptance_criteria": ["Entregar riscos"],
                            "depends_on": [0] if invalid else [],
                        }
                    ],
                },
                model="fake",
                provider="fake",
                usage=Usage(input_tokens=10, output_tokens=5),
                cost_usd=0.001,
                latency_ms=1,
            )

    router = RepairingRouter()
    outcome = await LLMPlanner(router).create_plan("Analise", {})

    assert len(router.requests) == 2
    assert "dependências inválidas" in router.requests[1].messages[0].content
    assert len(outcome.plan) == 1
    assert outcome.usage.tokens == 30
    assert outcome.usage.cost_usd == pytest.approx(0.002)


def consistency_plan(write_paths, protected="catalog.cjs"):
    return PlanOutput.model_validate({
        "rationale": "preservar contrato",
        "tasks": [{
            "title": "Editar", "description": "Mudança limitada",
            "capability": "backend", "write_paths": write_paths,
            "acceptance_criteria": [{
                "text": "Arquivo protegido", "kind": "file_unchanged", "path": protected,
            }],
        }],
    })


def test_factory_requires_edit_declaration_but_keeps_read_only_and_legacy_plans():
    factory = LLMPlanner(None, require_write_paths=True)
    with pytest.raises(PlanValidationError, match="sem write_paths"):
        factory._validate_plan_consistency(consistency_plan(None))
    factory._validate_plan_consistency(consistency_plan([]))
    factory._validate_plan_consistency(consistency_plan(["tests/catalog.test.cjs"]))
    LLMPlanner(None)._validate_plan_consistency(consistency_plan(None))


@pytest.mark.skipif(os.name != "posix", reason="lease da factory usa caminho POSIX")
def test_factory_runtime_enables_required_edit_declarations(tmp_path):
    from unittest.mock import Mock
    from app.api.container import LeaseBoundRuntimeFactory
    from app.infrastructure.settings import Settings
    from app.models.factory import RepositoryTarget, WorkspaceLease
    from app.providers.registry import ProviderRouter

    lease = WorkspaceLease(
        workflow_id="planner", repository=RepositoryTarget(full_name="fixture/test"),
        local_path=str(tmp_path), branch="forgehand/planner", base_sha="a" * 40,
    )
    router = Mock(spec=ProviderRouter)
    router.escalate.side_effect = lambda tier: tier
    planner = LeaseBoundRuntimeFactory(Settings(), router).build_planner(lease)
    with pytest.raises(PlanValidationError, match="sem write_paths"):
        planner._validate_plan_consistency(consistency_plan(None))


def test_protection_is_task_local_not_a_ban_on_later_tasks():
    plan = consistency_plan(["tests/catalog.test.cjs"])
    plan.tasks.extend(consistency_plan(["catalog.cjs"], protected="README.md").tasks)
    LLMPlanner(None, require_write_paths=True)._validate_plan_consistency(plan)


@pytest.mark.parametrize("path", ["catalog.cjs", "./catalog.cjs", "catalog.cjs/"])
def test_normalized_paths_cannot_hide_conflicts(path):
    with pytest.raises(PlanValidationError, match="contraditória"):
        LLMPlanner(None)._validate_plan_consistency(consistency_plan([path]))


@pytest.mark.parametrize("path", ["../catalog.cjs", "/catalog.cjs", "*.cjs", "a/../catalog.cjs", "a\\catalog.cjs", "", "."])
def test_planned_edits_require_exact_repository_relative_paths(path):
    with pytest.raises(PlanValidationError, match="caminhos relativos"):
        LLMPlanner(None)._validate_plan_consistency(consistency_plan([path]))


def test_explicit_modified_criterion_conflicts_even_in_legacy_plan():
    plan = consistency_plan(None)
    from app.models.task import AcceptanceCriterion
    plan.tasks[0].acceptance_criteria.append(AcceptanceCriterion(
        text="Modificar", kind="file_modified", path="catalog.cjs"
    ))
    with pytest.raises(PlanValidationError, match="contraditória"):
        LLMPlanner(None)._validate_plan_consistency(plan)


@pytest.mark.asyncio
async def test_contradictory_plan_exhausts_bounded_repair_without_returning_tasks():
    class Router:
        calls = 0

        async def complete(self, tier, request):
            self.calls += 1
            return CompletionResult(
                text="plan", parsed=consistency_plan(["catalog.cjs"]).model_dump(mode="json"),
                model="fake", provider="fake", usage=Usage(input_tokens=10, output_tokens=5),
                cost_usd=0.001, latency_ms=1,
            )

    router = Router()
    with pytest.raises(PlanValidationError, match="contraditória"):
        await LLMPlanner(router, max_validation_attempts=2).create_plan("Editar", {})
    assert router.calls == 2


@pytest.mark.asyncio
async def test_planner_repairs_write_and_unchanged_contradiction_before_execution():
    class RepairingRouter:
        def __init__(self):
            self.requests = []

        async def complete(self, tier, request):
            self.requests.append(request)
            return CompletionResult(
                text="plan",
                parsed={
                    "rationale": "Adicionar exportação sem alterar funções existentes",
                    "tasks": [{
                        "title": "Adicionar uniqueTags",
                        "description": "Adicionar e exportar uniqueTags em catalog.cjs preservando retail e wholesale.",
                        "capability": "backend",
                        "write_paths": ["./catalog.cjs", "tests/catalog.test.cjs"],
                        "acceptance_criteria": [{
                            "text": "As funções retail e wholesale não são alteradas",
                            "kind": "file_unchanged" if len(self.requests) == 1 else "subjective",
                            "path": "catalog.cjs",
                        }, {"text": "Testes passam", "kind": "tests_pass"}],
                    }],
                },
                model="fake", provider="fake",
                usage=Usage(input_tokens=10, output_tokens=5),
                cost_usd=0.001, latency_ms=1,
            )

    router = RepairingRouter()
    outcome = await LLMPlanner(router).create_plan("Adicionar uniqueTags", {})
    assert len(router.requests) == 2
    assert "catalog.cjs" in router.requests[1].messages[0].content
    assert "file_unchanged" in router.requests[1].messages[0].content
    assert outcome.plan[0].acceptance_criteria[0].kind.value == "subjective"
    assert outcome.usage.tokens == 30
    assert outcome.usage.cost_usd == pytest.approx(0.002)
