"""Critérios tipados: coerção e inferência legada, avaliação determinística por
tipo, judge que só chama o LLM para o subjetivo, planner com schema tipado."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from app.agents.criteria import (
    evaluate_objective_criteria,
    no_existing_file_modified,
)
from app.agents.judge import LLMJudge
from app.agents.planner import PlanOutput, PlannedTask, _task_stable_id
from app.agents.validation import ValidationSignal
from app.models.task import (
    AcceptanceCriterion,
    AgentTask,
    Capability,
    CriterionKind,
    format_criteria,
    infer_criterion_kind,
)
from app.providers.base import CompletionResult, Usage

WORKSPACE = {
    "applied_files": ["app/new.py", "app/old.py"],
    "file_diffs": [
        {
            "path": "app/new.py",
            "change_type": "created",
            "changed": True,
            "operation": "create",
        },
        {
            "path": "app/old.py",
            "change_type": "modified",
            "changed": True,
            "operation": "replace",
        },
        {
            "path": "app/same.py",
            "change_type": "unchanged",
            "changed": False,
            "operation": "replace",
        },
    ],
    "published_files": [
        {"path": "app/new.py", "content": "def add(a, b):\n    return a + b\n"},
        {"path": "app/old.py", "content": "VERSION = '2'\n"},
    ],
    "deleted_paths": [],
}


def _task(criteria, result=None, **overrides) -> AgentTask:
    base = dict(
        title="t",
        description="d",
        capability=Capability.BACKEND,
        acceptance_criteria=criteria,
        result=result,
    )
    base.update(overrides)
    return AgentTask(**base)


# --------------------------------------------------------------------------
# Modelo
# --------------------------------------------------------------------------


def test_string_criteria_are_coerced_with_legacy_inference():
    task = _task(
        [
            "Endpoints CRUD completos",
            "a alteração é mínima e restrita ao arquivo novo",
            "Citações válidas e grounding do repositório",
        ]
    )
    kinds = [c.kind for c in task.acceptance_criteria]
    assert kinds == [
        CriterionKind.SUBJECTIVE,
        CriterionKind.NO_EXISTING_FILE_MODIFIED,
        CriterionKind.CITATIONS_VALID,
    ]
    assert task.acceptance_criteria[0].text == "Endpoints CRUD completos"
    assert infer_criterion_kind("Testes cobrem CRUD") is CriterionKind.SUBJECTIVE


def test_criterion_parameters_are_required_per_kind():
    with pytest.raises(ValidationError, match="exige `path`"):
        AcceptanceCriterion(text="cria", kind=CriterionKind.FILE_CREATED)
    with pytest.raises(ValidationError, match="exige `pattern`"):
        AcceptanceCriterion(text="c", kind=CriterionKind.CONTENT_CONTAINS, path="a")
    with pytest.raises(ValidationError, match="exige `paths`"):
        AcceptanceCriterion(text="c", kind=CriterionKind.CHANGES_LIMITED_TO)
    ok = AcceptanceCriterion(
        text="README menciona setup",
        kind=CriterionKind.CONTENT_CONTAINS,
        path="README.md",
        pattern="## Setup",
    )
    assert (
        ok.label
        == "README menciona setup [content_contains: README.md; pattern='## Setup']"
    )


def test_format_criteria_marks_objective_kinds_only():
    task = _task(
        [
            {"text": "código limpo", "kind": "subjective"},
            {"text": "testes verdes", "kind": "tests_pass"},
            {"text": "só em app/", "kind": "changes_limited_to", "paths": ["app/*"]},
        ]
    )
    assert format_criteria(task.acceptance_criteria).splitlines() == [
        "- código limpo",
        "- testes verdes [tests_pass]",
        "- só em app/ [changes_limited_to: app/*]",
    ]


def test_task_dump_and_reload_preserves_typed_criteria():
    task = _task([{"text": "x", "kind": "file_created", "path": "a.py"}])
    reloaded = AgentTask.model_validate(task.model_dump(mode="json"))
    assert reloaded.acceptance_criteria[0].kind is CriterionKind.FILE_CREATED
    assert reloaded.acceptance_criteria[0].path == "a.py"


# --------------------------------------------------------------------------
# Avaliação determinística
# --------------------------------------------------------------------------


def _verdicts(criteria, result=None, signals=None, context=None):
    task = _task(criteria, result=result)
    return {
        v.criterion.text: v
        for v in evaluate_objective_criteria(task, context or {}, signals or {})
    }


def test_file_criteria_against_workspace_diffs():
    result = {"summary": "s", "workspace": WORKSPACE}
    verdicts = _verdicts(
        [
            {"text": "new criado", "kind": "file_created", "path": "app/new.py"},
            {"text": "old criado", "kind": "file_created", "path": "app/old.py"},
            {"text": "ghost criado", "kind": "file_created", "path": "ghost.py"},
            {"text": "old alterado", "kind": "file_modified", "path": "app/old.py"},
            {"text": "new alterado", "kind": "file_modified", "path": "app/new.py"},
            {"text": "só novos", "kind": "no_existing_file_modified"},
            {"text": "só app", "kind": "changes_limited_to", "paths": ["app/*"]},
            {"text": "só new", "kind": "changes_limited_to", "paths": ["app/new.py"]},
        ],
        result=result,
    )
    assert verdicts["new criado"].passed is True
    assert verdicts["old criado"].passed is False
    assert "já existia" in verdicts["old criado"].detail
    assert verdicts["ghost criado"].passed is False
    assert "op=create" in (verdicts["ghost criado"].required_change or "")
    assert verdicts["old alterado"].passed is True
    assert verdicts["new alterado"].passed is False
    assert verdicts["só novos"].passed is False
    assert "app/old.py (replace)" in verdicts["só novos"].detail
    assert verdicts["só app"].passed is True
    assert verdicts["só new"].passed is False
    assert "app/old.py" in verdicts["só new"].detail


def test_content_contains_uses_published_content():
    result = {"summary": "s", "workspace": WORKSPACE}
    verdicts = _verdicts(
        [
            {
                "text": "add existe",
                "kind": "content_contains",
                "path": "app/new.py",
                "pattern": r"def add\(",
            },
            {
                "text": "sub existe",
                "kind": "content_contains",
                "path": "app/new.py",
                "pattern": r"def sub\(",
            },
            {
                "text": "readme",
                "kind": "content_contains",
                "path": "README.md",
                "pattern": "x",
            },
            {
                "text": "regex ruim",
                "kind": "content_contains",
                "path": "app/new.py",
                "pattern": "(",
            },
        ],
        result=result,
    )
    assert verdicts["add existe"].passed is True
    assert verdicts["sub existe"].passed is False
    assert verdicts["readme"].passed is False
    assert "não está entre os arquivos publicados" in verdicts["readme"].detail
    assert verdicts["regex ruim"].passed is False
    assert "regex inválida" in verdicts["regex ruim"].detail


def test_workspace_criteria_are_unverifiable_without_runtime():
    verdicts = _verdicts(
        [
            {"text": "novo", "kind": "file_created", "path": "a.py"},
            {"text": "só novos", "kind": "no_existing_file_modified"},
        ],
        result={"summary": "sem workspace"},
    )
    assert all(v.passed is None for v in verdicts.values())
    assert no_existing_file_modified(_task(["x"], result=None)) is None


def test_signal_criteria_follow_validators_or_are_unverifiable():
    signals = {
        "pytest": ValidationSignal(
            name="pytest", passed=False, details="1 failed", exit_code=1
        ),
        "ruff": ValidationSignal(name="ruff", passed=True),
    }
    verdicts = _verdicts(
        [
            {"text": "testes", "kind": "tests_pass"},
            {"text": "lint", "kind": "lint_pass"},
            {"text": "tipos", "kind": "types_pass"},
        ],
        signals=signals,
    )
    assert verdicts["testes"].passed is False
    assert verdicts["testes"].required_change == "Faça `pytest` passar: 1 failed"
    assert verdicts["lint"].passed is True
    assert verdicts["tipos"].passed is None
    assert "indisponível" in verdicts["tipos"].detail


def test_citations_criterion_checks_grounding_scope():
    context = {
        "repository_grounding": {
            "repo_root": "/r",
            "require_citations": True,
            "evidence": [
                {
                    "id": "E1",
                    "path": "a.py",
                    "line_start": 1,
                    "line_end": 1,
                    "excerpt": "x",
                },
                {
                    "id": "E2",
                    "path": "b.py",
                    "line_start": 1,
                    "line_end": 1,
                    "excerpt": "y",
                },
            ],
        }
    }
    criteria = [{"text": "citações válidas", "kind": "citations_valid"}]
    task_ok = _task(criteria, result={"citations": ["E1"]}, evidence_ids=["E1"])
    task_bad = _task(criteria, result={"citations": ["E2"]}, evidence_ids=["E1"])
    ok = evaluate_objective_criteria(task_ok, context, {})[0]
    bad = evaluate_objective_criteria(task_bad, context, {})[0]
    none = evaluate_objective_criteria(task_ok, {}, {})[0]
    assert ok.passed is True
    assert bad.passed is False and "fora do escopo" in bad.detail
    assert none.passed is None


# --------------------------------------------------------------------------
# Judge
# --------------------------------------------------------------------------


class RecordingRouter:
    def __init__(self, payload):
        self._payload = payload
        self.requests = []

    async def complete(self, tier, request):
        self.requests.append(request)
        return CompletionResult(
            text="ok",
            parsed=self._payload,
            model="fake",
            provider="fake",
            usage=Usage(input_tokens=7, output_tokens=0),
            cost_usd=0.001,
            latency_ms=0.0,
        )


def _llm_verdict(*criteria, approved=True, failures=(), required=()):
    return {
        "criteria": [
            {"index": i, "criterion": text, "score": score, "reasoning": "r"}
            for i, (text, score) in enumerate(criteria, start=1)
        ],
        "failures": list(failures),
        "required_changes": list(required),
        "overall_score": 0.9 if approved else 0.3,
        "approved": approved,
    }


@pytest.mark.asyncio
async def test_judge_skips_llm_when_all_criteria_are_objective():
    router = RecordingRouter(_llm_verdict())
    task = _task(
        [
            {"text": "new criado", "kind": "file_created", "path": "app/new.py"},
            {"text": "só app", "kind": "changes_limited_to", "paths": ["app/*"]},
        ],
        result={"summary": "s", "workspace": WORKSPACE},
    )
    outcome = await LLMJudge(router).evaluate(task, {})
    assert router.requests == [], "nenhuma chamada de LLM"
    assert outcome.evaluation.approved is True
    assert outcome.evaluation.criteria_scores == {"new criado": 1.0, "só app": 1.0}
    assert outcome.evaluation.validated_by == ["criteria"]
    assert outcome.usage.tokens == 0


@pytest.mark.asyncio
async def test_judge_sends_only_subjective_criteria_and_matches_by_index():
    router = RecordingRouter(_llm_verdict(("código legível", 0.95)))
    task = _task(
        [
            {"text": "new criado", "kind": "file_created", "path": "app/new.py"},
            {"text": "código legível", "kind": "subjective"},
        ],
        result={"summary": "s", "workspace": WORKSPACE},
    )
    outcome = await LLMJudge(router).evaluate(task, {})
    prompt = router.requests[0].messages[0].content
    assert "1. código legível" in prompt
    assert (
        "new criado"
        not in prompt.split("Critérios de aceitação a avaliar:")[1].split("Resultado")[
            0
        ]
    )
    assert outcome.evaluation.approved is True
    assert outcome.evaluation.criteria_scores == {
        "new criado": 1.0,
        "código legível": 0.95,
    }
    assert outcome.evaluation.validated_by == ["llm", "criteria"]
    assert outcome.usage.tokens == 7


@pytest.mark.asyncio
async def test_objective_failure_vetoes_even_if_llm_approves():
    router = RecordingRouter(_llm_verdict(("código legível", 1.0)))
    task = _task(
        [
            {"text": "ghost criado", "kind": "file_created", "path": "ghost.py"},
            {"text": "código legível", "kind": "subjective"},
        ],
        result={"summary": "s", "workspace": WORKSPACE},
    )
    outcome = await LLMJudge(router).evaluate(task, {})
    ev = outcome.evaluation
    assert ev.approved is False
    assert ev.criteria_scores["ghost criado"] == 0.0
    assert ev.failures == ["[file_created] `ghost.py` não foi tocado pela tarefa."]
    assert ev.required_changes == ["Crie o arquivo `ghost.py` (op=create)."]
    assert ev.score == pytest.approx(0.5)


@pytest.mark.asyncio
async def test_llm_failures_count_only_when_it_rejected_its_own_criteria():
    # LLM aprova o critério dele mas devolve failures sobre um critério objetivo
    # (que não estava na lista): ruído descartado.
    noisy = RecordingRouter(
        _llm_verdict(
            ("arquivo existe", 1.0),
            approved=False,
            failures=["O arquivo foi modificado após a criação (alteração mínima)."],
            required=["Recrie o arquivo."],
        )
    )
    task = _task(
        ["arquivo existe", "a alteração é mínima e restrita ao arquivo novo"],
        result={
            "summary": "s",
            "workspace": {
                "file_diffs": [
                    {"path": "gen/x.py", "change_type": "created", "changed": True}
                ]
            },
        },
    )
    outcome = await LLMJudge(noisy).evaluate(task, {})
    assert outcome.evaluation.approved is True
    assert outcome.evaluation.failures == []
    assert outcome.evaluation.required_changes == []

    # LLM reprova o critério dele: failures e required_changes entram
    strict = RecordingRouter(
        _llm_verdict(
            ("arquivo existe", 0.2),
            approved=False,
            failures=["arquivo vazio"],
            required=["preencha o arquivo"],
        )
    )
    outcome = await LLMJudge(strict).evaluate(task, {})
    assert outcome.evaluation.approved is False
    assert outcome.evaluation.failures == ["arquivo vazio"]
    assert outcome.evaluation.required_changes == ["preencha o arquivo"]


@pytest.mark.asyncio
async def test_unverifiable_objective_criterion_falls_back_to_llm_with_note():
    router = RecordingRouter(_llm_verdict(("testes verdes", 0.9)))
    task = _task(
        [{"text": "testes verdes", "kind": "tests_pass"}],
        result={"summary": "s"},
    )
    outcome = await LLMJudge(router).evaluate(task, {})
    prompt = router.requests[0].messages[0].content
    assert (
        "1. testes verdes (não verificável automaticamente: sinal `pytest` indisponível"
        in prompt
    )
    assert outcome.evaluation.criteria_scores == {"testes verdes": 0.9}
    assert outcome.evaluation.validated_by == ["llm"]


@pytest.mark.asyncio
async def test_unmatched_subjective_criterion_never_passes_by_omission():
    router = RecordingRouter(
        {
            "criteria": [{"criterion": "outro nome", "score": 1.0, "reasoning": "r"}],
            "failures": [],
            "required_changes": [],
            "overall_score": 0.3,
            "approved": False,
        }
    )
    task = _task(["clareza"], result={"summary": "s"})
    outcome = await LLMJudge(router).evaluate(task, {})
    assert outcome.evaluation.criteria_scores == {"clareza": 0.0}
    assert outcome.evaluation.approved is False


# --------------------------------------------------------------------------
# Planner
# --------------------------------------------------------------------------


def test_planner_schema_exposes_typed_criteria_and_coerces_strings():
    schema = json.dumps(PlanOutput.model_json_schema())
    for kind in ("tests_pass", "file_created", "content_contains", "subjective"):
        assert f'"{kind}"' in schema
    planned = PlannedTask(
        title="t",
        description="d",
        capability=Capability.BACKEND,
        acceptance_criteria=["Endpoints CRUD completos"],
    )
    assert planned.acceptance_criteria[0].kind is CriterionKind.SUBJECTIVE


def test_planner_stable_id_is_stable_across_string_and_typed_forms():
    typed = PlannedTask(
        title="t",
        description="d",
        capability=Capability.BACKEND,
        acceptance_criteria=[{"text": "ok", "kind": "subjective"}],
    )
    as_string = PlannedTask(
        title="t",
        description="d",
        capability=Capability.BACKEND,
        acceptance_criteria=["ok"],
    )
    assert _task_stable_id(typed) == _task_stable_id(as_string)
    other = typed.model_copy(
        update={
            "acceptance_criteria": [
                AcceptanceCriterion(text="ok", kind=CriterionKind.TESTS_PASS)
            ]
        }
    )
    assert _task_stable_id(other) != _task_stable_id(typed)


def test_file_unchanged_passes_only_when_path_is_untouched():
    result = {"summary": "s", "workspace": WORKSPACE}
    verdicts = _verdicts(
        [
            {"text": "testes intactos", "kind": "file_unchanged", "path": "tests/x.py"},
            {"text": "same intacto", "kind": "file_unchanged", "path": "app/same.py"},
            {"text": "old intacto", "kind": "file_unchanged", "path": "app/old.py"},
        ],
        result=result,
    )
    assert verdicts["testes intactos"].passed is True
    assert verdicts["same intacto"].passed is True  # diff registrado sem mudança
    assert verdicts["old intacto"].passed is False
    assert "foi alterado (replace)" in verdicts["old intacto"].detail
    with pytest.raises(ValidationError, match="exige `path`"):
        AcceptanceCriterion(text="x", kind=CriterionKind.FILE_UNCHANGED)


def test_planner_prompt_declares_non_writing_capabilities():
    from app.agents.planner import LLMPlanner

    class Router:
        async def complete(self, tier, request):
            raise AssertionError("não chamado")

    plain = LLMPlanner(Router())._system_prompt()
    assert "NÃO gravam arquivos" not in plain

    scoped = LLMPlanner(
        Router(), non_writing_capabilities={Capability.RESEARCH, Capability.REVIEW}
    )._system_prompt()
    assert "NÃO gravam arquivos (research, review)" in scoped
    assert "use `documentation`" in scoped

    disabled = LLMPlanner(Router(), apply_files_enabled=False)._system_prompt()
    assert "NENHUMA tarefa grava arquivos" in disabled


def test_executor_criteria_include_the_exact_content_pattern():
    from app.models.task import AcceptanceCriterion, CriterionKind, format_criteria

    criterion = AcceptanceCriterion(
        text="Output uses EUR",
        kind=CriterionKind.CONTENT_CONTAINS,
        path="README.md",
        pattern=r"EUR\s+12\.00",
    )
    assert repr(criterion.pattern) in format_criteria([criterion])
