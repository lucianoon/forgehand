"""E2E: grafo completo + agentes reais + SDK Anthropic com HTTP mockado.

O mock roteia pela combinacao (tool schema pedido, conteudo do system):
- PlanOutput -> devolve plano de 3 tarefas com dependencia;
- ExecutionOutput -> devolve artefatos;
- JudgeOutput -> rejeita 'backend' na 1a avaliacao, aprova depois
  (exercita replan com feedback real do judge).
"""

import json
import sys

sys.path.insert(0, ".")
import anthropic
import httpx
import pytest
from langgraph.checkpoint.memory import MemorySaver
from app.graph.workflow import build_workflow, build_serde
from app.models.task import TaskBudget, TaskStatus
from app.providers.base import CompletionResult, ModelPricing, Usage
from app.providers.anthropic_provider import AnthropicProvider
from app.providers.registry import ModelTier, ProviderRouter, TierBinding
from app.agents.planner import (
    LLMPlanner,
    PlanOutput,
    PlanValidationError,
    PlannedTask,
    _to_agent_tasks,
)
from app.agents.judge import LLMJudge
from app.agents.validation import ValidationSignal
from app.agents.registry import CapabilityExecutorRegistry

PRICING = {
    "claude-sonnet-4-6": ModelPricing(input_per_mtok=3.0, output_per_mtok=15.0),
    "claude-haiku-4-5": ModelPricing(input_per_mtok=1.0, output_per_mtok=5.0),
}

state = {
    "judge_calls": {},
    "models_used": set(),
    "executor_saw_deps": False,
    "planner_saw_grounding": False,
    "executor_saw_grounding": False,
}


def tool_result(payload, model, in_tok=800, out_tok=400):
    return httpx.Response(
        200,
        json={
            "id": "m",
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": [
                {
                    "type": "tool_use",
                    "id": "t",
                    "name": "emit_structured_output",
                    "input": payload,
                }
            ],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": in_tok, "output_tokens": out_tok},
        },
    )


def handler(request):
    body = json.loads(request.content)
    schema_props = body["tools"][0]["input_schema"].get("properties", {})
    model = body["model"]
    state["models_used"].add(model)
    user = body["messages"][-1]["content"]

    if "rationale" in schema_props and "tasks" in schema_props:  # PlanOutput
        assert "Grounding obrigatório do repositório" in user
        assert "[E1] app/main.py:1-3" in user
        state["planner_saw_grounding"] = True
        return tool_result(
            {
                "rationale": "vertical mvp",
                "tasks": [
                    {
                        "title": "backend",
                        "description": "CRUD de clientes em FastAPI",
                        "capability": "backend",
                        "acceptance_criteria": [
                            "Endpoints CRUD completos",
                            "Modelos SQLAlchemy",
                        ],
                        "evidence_ids": ["E1"],
                        "depends_on": [],
                        "is_critical": False,
                    },
                    {
                        "title": "documentacao",
                        "description": "README do projeto",
                        "capability": "documentation",
                        "acceptance_criteria": ["README com setup"],
                        "evidence_ids": ["E2"],
                        "depends_on": [],
                        "is_critical": False,
                    },
                    {
                        "title": "testes",
                        "description": "pytest do CRUD",
                        "capability": "testing",
                        "acceptance_criteria": ["Testes cobrem CRUD"],
                        "evidence_ids": ["E3"],
                        "depends_on": [0],
                        "is_critical": False,
                    },
                ],
            },
            model,
        )

    if "files" in schema_props:  # ExecutionOutput
        if "Resultados das dependências" in user:
            state["executor_saw_deps"] = True
        if "Grounding obrigatório do repositório" in user:
            state["executor_saw_grounding"] = True
        if "Correções exigidas pelo judge" in user:
            assert "endpoint DELETE" in user, (
                "feedback do judge deveria estar na descricao"
            )
        title = user.split("Tarefa: ")[1].split("\n")[0]
        citations_by_title = {
            "backend": ["E1"],
            "documentacao": ["E2"],
            "testes": ["E3"],
        }
        return tool_result(
            {
                "summary": f"{title} implementado",
                "files": [{"path": f"app/{title}.py", "content": f"# {title}"}],
                "notes": [],
                "citations": citations_by_title[title],
            },
            model,
        )

    if "criteria" in schema_props:  # JudgeOutput
        title = user.split("Tarefa: ")[1].split("\n")[0]
        n = state["judge_calls"][title] = state["judge_calls"].get(title, 0) + 1
        if title == "backend" and n == 1:
            return tool_result(
                {
                    "criteria": [
                        {
                            "criterion": "Endpoints CRUD completos",
                            "score": 0.3,
                            "reasoning": "falta DELETE",
                        }
                    ],
                    "failures": ["DELETE ausente"],
                    "required_changes": ["adicionar endpoint DELETE"],
                    "overall_score": 0.3,
                    "approved": False,
                },
                model,
            )
        return tool_result(
            {
                "criteria": [{"criterion": "ok", "score": 0.9, "reasoning": "atende"}],
                "failures": [],
                "required_changes": [],
                "overall_score": 0.9,
                "approved": True,
            },
            model,
        )

    raise AssertionError(f"schema inesperado: {list(schema_props)}")


class FakeMemory:
    def __init__(self):
        self.persisted = None

    async def load_context(self, project_id, request=""):
        return {
            "stack": "FastAPI + PostgreSQL",
            "repository_grounding": {
                "repo_root": "/repo",
                "require_citations": True,
                "top_level_entries": ["app", "tests", "README.md"],
                "evidence": [
                    {
                        "id": "E1",
                        "path": "app/main.py",
                        "line_start": 1,
                        "line_end": 3,
                        "excerpt": "from fastapi import FastAPI",
                    },
                    {
                        "id": "E2",
                        "path": "README.md",
                        "line_start": 1,
                        "line_end": 3,
                        "excerpt": "# Forgehand",
                    },
                    {
                        "id": "E3",
                        "path": "tests/test_api.py",
                        "line_start": 1,
                        "line_end": 3,
                        "excerpt": "def test_api():",
                    },
                ],
            },
        }

    async def persist(self, s):
        self.persisted = s


@pytest.mark.asyncio
async def test_agents_e2e():
    client = anthropic.AsyncAnthropic(
        api_key="test",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    provider = AnthropicProvider(PRICING, client=client, base_backoff_seconds=0.01)
    router = ProviderRouter(
        providers={"anthropic": provider},
        bindings={
            ModelTier.FAST: TierBinding(
                provider_name="anthropic", model="claude-haiku-4-5"
            ),
            ModelTier.STANDARD: TierBinding(
                provider_name="anthropic", model="claude-sonnet-4-6"
            ),
            ModelTier.STRONG: TierBinding(
                provider_name="anthropic", model="claude-opus-4-8"
            ),
        },
    )
    mem = FakeMemory()
    app = build_workflow(
        planner=LLMPlanner(router),
        registry=CapabilityExecutorRegistry(router),
        judge=LLMJudge(router),
        memory=mem,
        checkpointer=MemorySaver(serde=build_serde()),
    )
    cfg = {"configurable": {"thread_id": "e2e"}}
    out = await app.ainvoke(
        {
            "request": "Crie uma API FastAPI de clientes",
            "project_id": "demo",
            "workflow_id": "w-e2e",
            "owner_client_id": "client-demo",
        },
        cfg,
    )

    assert out["phase"].value == "completed"
    plan = out["plan"]
    assert all(t.status == TaskStatus.COMPLETED for t in plan)
    assert state["planner_saw_grounding"] is True
    assert state["executor_saw_grounding"] is True
    print("1. e2e OK — fluxo completo com agentes reais, phase=completed")

    backend = next(t for t in plan if t.title == "backend")
    assert backend.attempt_count == 2, (
        "backend deveria ter 2 tentativas (rejeicao + correcao)"
    )
    assert "endpoint DELETE" in backend.description, "feedback do judge anexado"
    docs = next(t for t in plan if t.title == "documentacao")
    assert docs.attempt_count == 1, "docs aprovada de primeira nao re-executa"
    print(
        "2. replan real OK — judge rejeitou, executor corrigiu com o feedback, docs intocada"
    )

    assert state["executor_saw_deps"], "testes deveria receber resultado do backend"
    print("3. dependency_results OK — executor de testes recebeu artefatos do backend")

    assert backend.evidence_ids == ["E1"]
    assert backend.result["citations"] == ["E1"]
    print("3b. grounding OK — planner e executor citaram evidence_ids reais")

    assert "claude-haiku-4-5" in state["models_used"], (
        "docs (tier FAST) deveria usar haiku"
    )
    assert "claude-sonnet-4-6" in state["models_used"]
    assert "claude-opus-4-8" not in state["models_used"], (
        "STRONG so por escalonamento (regra 9)"
    )
    print(
        "4. tiers OK — FAST p/ docs, STANDARD p/ resto, STRONG nunca sem escalonamento"
    )

    assert out["usage"]["tokens"] == 10_800
    assert out["usage"]["cost_usd"] > 0
    per_task = sum(t.budget.consumed_cost_usd for t in plan)
    assert abs(per_task - sum(a.cost_usd for t in plan for a in t.attempts)) < 1e-9
    print(
        f"5. custo OK — {out['usage']['tokens']} tokens, {out['usage']['cost_usd']:.4f} USD rastreados por tarefa e no agregado"
    )

    # 6. Planner rejeita plano ciclico / indice invalido
    try:
        _to_agent_tasks(
            PlanOutput(
                rationale="x",
                tasks=[
                    PlannedTask(
                        title="a",
                        description="d",
                        capability="backend",
                        acceptance_criteria=["c"],
                        depends_on=[1],
                    ),
                    PlannedTask(
                        title="b",
                        description="d",
                        capability="backend",
                        acceptance_criteria=["c"],
                        depends_on=[0],
                    ),
                ],
            )
        )
        raise SystemExit("FALHOU: ciclo aceito")
    except PlanValidationError:
        pass
    try:
        _to_agent_tasks(
            PlanOutput(
                rationale="x",
                tasks=[
                    PlannedTask(
                        title="a",
                        description="d",
                        capability="backend",
                        acceptance_criteria=["c"],
                        depends_on=[9],
                    ),
                ],
            )
        )
        raise SystemExit("FALHOU: indice invalido aceito")
    except PlanValidationError:
        pass
    print(
        "6. validação de plano OK — ciclos e índices inválidos barrados antes do grafo"
    )

    # 7. Judge com validador objetivo reprovando veta aprovacao do LLM
    class FailingPytest:
        name = "pytest"

        async def validate(self, task):
            return ValidationSignal(name="pytest", passed=False, details="2 failed")

    judge = LLMJudge(router, validators=[FailingPytest()])
    ev = (await judge.evaluate(backend, {})).evaluation
    assert ev.approved is False and ev.tests_passed is False
    assert ev.score <= 0.4 and "pytest" in ev.validated_by
    assert any("[pytest]" in f for f in ev.failures)
    print("7. veto objetivo OK — pytest reprovando derruba aprovação do LLM")

    print("\n7/7 cenarios e2e de agentes OK")


def test_to_agent_tasks_is_stable_for_identical_plan():
    plan = PlanOutput(
        rationale="x",
        tasks=[
            PlannedTask(
                title="analisar",
                description="mapear arquitetura",
                capability="architecture",
                acceptance_criteria=["citar evidencias"],
                evidence_ids=["E1"],
                depends_on=[],
            )
        ],
    )

    first = _to_agent_tasks(plan)
    second = _to_agent_tasks(plan)

    assert first[0].id == second[0].id


@pytest.mark.asyncio
async def test_planner_applies_default_task_budget():
    class StaticRouter:
        async def complete(self, tier, request):
            return CompletionResult(
                text="ok",
                parsed={
                    "rationale": "x",
                    "tasks": [
                        {
                            "title": "analisar",
                            "description": "mapear arquitetura",
                            "capability": "architecture",
                            "acceptance_criteria": ["citar evidencias"],
                            "evidence_ids": ["E1"],
                            "depends_on": [],
                            "is_critical": False,
                        }
                    ],
                },
                model="fake",
                provider="fake",
                usage=Usage(),
                cost_usd=0.0,
                latency_ms=0.0,
            )

    planner = LLMPlanner(
        StaticRouter(),
        default_task_budget=TaskBudget(max_tokens=1234, max_cost_usd=0.77),
    )
    outcome = await planner.create_plan(
        "Analise o repositório",
        {
            "repository_grounding": {
                "repo_root": "/repo",
                "require_citations": True,
                "evidence": [
                    {
                        "id": "E1",
                        "path": "app/main.py",
                        "line_start": 1,
                        "line_end": 2,
                        "excerpt": "from fastapi import FastAPI",
                    }
                ],
            }
        },
    )

    assert outcome.plan[0].budget.max_tokens == 1234
    assert outcome.plan[0].budget.max_cost_usd == 0.77
