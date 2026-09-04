"""Memória que aprende, escalonamento do planner, replace reindentado,
palavras normalizadas, TOML, sandbox em produção e critérios no PR."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from app.agents.planner import LLMPlanner
from app.graph.phase_delivery import criteria_details
from app.graph.state import WorkflowState
from app.infrastructure.memory import InMemoryProjectMemory, workflow_summary
from app.infrastructure.repository_grounding import RepositoryGroundingCollector, _keywordize
from app.infrastructure.settings import Settings
from app.infrastructure.workspace_runtime import OperationApplyError, apply_replace
from app.models.task import AgentTask, Capability, EvaluationResult, TaskStatus
from app.providers.base import CompletionResult, Usage
from app.providers.registry import ModelTier


def _state_with_failures() -> WorkflowState:
    task = AgentTask(title="Criar API", description="d", capability=Capability.BACKEND, acceptance_criteria=["ok"])
    good = AgentTask(title="Docs", description="d", capability=Capability.DOCUMENTATION, acceptance_criteria=["ok"])
    return WorkflowState(
        workflow_id=str(uuid4()),
        project_id="p",
        owner_client_id="dev",
        request="r",
        plan=[task, good],
        evaluations=[
            EvaluationResult(task_id=task.id, approved=False, score=0.2, criteria_scores={"ok": 0.2}, failures=["faltou tratar erro 404", "sem teste do caminho de falha", "terceira"], required_changes=[]),
            EvaluationResult(task_id=good.id, approved=True, score=1.0, criteria_scores={"ok": 1.0}, failures=[], required_changes=[]),
        ],
    )


@pytest.mark.asyncio
async def test_memory_learns_lessons_from_judge_failures() -> None:
    state = _state_with_failures()
    summary = workflow_summary(state)
    assert summary["lessons"] == ["backend: faltou tratar erro 404", "backend: sem teste do caminho de falha"]

    memory = InMemoryProjectMemory(Settings(_env_file=None, repository_grounding_enabled=False))
    await memory.persist(state)
    await memory.persist(state)  # repetido não duplica
    context = await memory.load_context("p", "novo pedido")
    assert context["project_memory"]["lessons"] == summary["lessons"]
    assert "lessons" not in context["project_memory"]["recent_workflows"][0]


class _TierRouter:
    def __init__(self) -> None:
        self.tiers: list[ModelTier] = []

    async def complete(self, tier, request):
        self.tiers.append(tier)
        contradictory = len(self.tiers) == 1
        task = {
            "title": "Editar módulo",
            "description": "d",
            "capability": "backend",
            "acceptance_criteria": [{"text": "intacto", "kind": "file_unchanged", "path": "a.py"}] if contradictory else [{"text": "ok", "kind": "subjective"}],
            "write_paths": ["a.py"],
        }
        return CompletionResult(
            text="", parsed={"rationale": "r", "tasks": [task]}, tool_calls=[], model="m", provider="fake",
            usage=Usage(input_tokens=1, output_tokens=1), cost_usd=0.0, latency_ms=1,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("escalate,expected", [(True, [ModelTier.STANDARD, ModelTier.STRONG]), (False, [ModelTier.STANDARD, ModelTier.STANDARD])])
async def test_planner_escalates_tier_only_on_structural_retry(escalate, expected) -> None:
    router = _TierRouter()
    planner = LLMPlanner(router, max_validation_attempts=2, escalate_on_retry=escalate)  # type: ignore[arg-type]
    outcome = await planner.create_plan("Editar o módulo a.py mantendo a API", {})
    assert len(outcome.plan) == 1 and router.tiers == expected
    assert LLMPlanner(router, tier=ModelTier.STRONG)._tier_for_attempt(3) is ModelTier.STRONG  # type: ignore[arg-type]


def test_apply_replace_reindents_when_indentation_differs() -> None:
    before = "class A:\n    def f(self):\n        x = 1\n        return x\n"
    # trecho copiado de uma evidência com 4 espaços a menos
    search = "def f(self):\n    x = 1\n    return x"
    replacement = "def f(self):\n    x = 2\n    return x * 2"
    after = apply_replace(before, search, replacement, None)
    assert after == "class A:\n    def f(self):\n        x = 2\n        return x * 2\n"
    # exato continua exato; ambiguidade continua erro
    assert apply_replace("a\nb\n", "a", "z", None) == "z\nb\n"
    with pytest.raises(OperationApplyError, match="2 vezes"):
        apply_replace("  x\n  y\n\n  x\n  y\n", "x\ny", "q", None)


def test_keywords_ignore_accents_and_common_suffixes(tmp_path: Path) -> None:
    assert _keywordize("Corrigir a função de divisão e os testes rapidamente") == ["corrigir", "funcao", "divisao", "test", "rapid"]
    (tmp_path / "calc").mkdir()
    (tmp_path / "calc" / "operacoes.py").write_text("def divisao(a, b):\n    return a / b  # funcao basica\n", encoding="utf-8")
    (tmp_path / "calc" / "outro.py").write_text("VALOR = 1\n", encoding="utf-8")
    paths = [e["path"] for e in RepositoryGroundingCollector(str(tmp_path)).collect("Corrigir a função de divisão")["evidence"]]
    assert paths == ["calc/operacoes.py"]


def test_toml_config_is_read_below_environment(tmp_path: Path, monkeypatch) -> None:
    config = tmp_path / "forgehand.toml"
    config.write_text('app_name = "do-toml"\nplanner_tier = 3\n', encoding="utf-8")
    monkeypatch.setenv("FORGEHAND_CONFIG", str(config))
    monkeypatch.delenv("APP_NAME", raising=False)
    settings = Settings(_env_file=None)
    assert settings.app_name == "do-toml" and settings.planner_tier == 3
    monkeypatch.setenv("APP_NAME", "do-ambiente")
    assert Settings(_env_file=None).app_name == "do-ambiente"
    monkeypatch.setenv("FORGEHAND_CONFIG", str(tmp_path / "inexistente.toml"))
    monkeypatch.delenv("APP_NAME", raising=False)
    assert Settings(_env_file=None).app_name == "forgehand"


def test_production_requires_docker_for_executor_commands() -> None:
    keys = '{"k":{"client_id":"c","projects":["*"],"role":"admin"}}'
    with pytest.raises(ValueError, match="docker"):
        Settings(_env_file=None, environment="prod", api_keys_json=keys, executor_apply_files_enabled=True)
    with pytest.raises(ValueError, match="docker"):
        Settings(_env_file=None, environment="prod", api_keys_json=keys, agent_tools_allow_commands=True)
    assert Settings(_env_file=None, environment="prod", api_keys_json=keys, executor_apply_files_enabled=True, executor_command_backend="docker").environment == "prod"
    assert Settings(_env_file=None, environment="prod", api_keys_json=keys).executor_command_backend == "local"
    # conftest fixa EXECUTOR_MAX_AUTOCORRECT_ROUNDS=0 no ambiente; o default do campo é 1
    assert Settings.model_fields["executor_max_autocorrect_rounds"].default == 1


def test_criteria_details_table_for_pull_request_body() -> None:
    state = _state_with_failures()
    completed = [state.plan[1].model_copy(update={"status": TaskStatus.COMPLETED})]
    table = criteria_details(state, completed)
    assert "| Docs | ok | 1.00 | llm |" in table and table.startswith("## Critérios verificados")
    assert criteria_details(state, []) == ""
