"""Critérios objetivos sobre o texto entregue por tarefas que não gravam arquivo."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.agents.criteria import evaluate_objective_criteria, task_output_text
from app.models.task import AcceptanceCriterion, AgentTask, Capability, CriterionKind


def _task(criteria, result):
    return AgentTask(
        title="t",
        description="d",
        capability=Capability.RESEARCH,
        acceptance_criteria=criteria,
        result=result,
    )


def _verdicts(criteria, result):
    task = _task(criteria, result)
    return {v.criterion.text: v for v in evaluate_objective_criteria(task, {}, {})}


RESULT = {
    "summary": "Análise concluída em três pontos.",
    "notes": ["PONTO 1 — limitação.", "PONTO 2 — conteúdo.", "PONTO 3 — recomendação."],
    "citations": ["E1"],
    "exploration": {"trace": [{"name": "read_file", "preview": "segredo do trace"}]},
}


def test_output_text_uses_summary_and_notes_only() -> None:
    text = task_output_text(_task(["ok"], RESULT))
    assert text.startswith("Análise concluída")
    assert "PONTO 3" in text
    assert "segredo do trace" not in text and "E1" not in text
    assert task_output_text(_task(["ok"], None)) == ""


def test_output_contains_and_min_chars_are_decided_by_code() -> None:
    verdicts = _verdicts(
        [
            {"text": "tem três pontos", "kind": "output_contains", "pattern": r"PONTO 3"},
            {"text": "menciona banco", "kind": "output_contains", "pattern": r"postgres"},
            {"text": "tamanho ok", "kind": "output_min_chars", "min_chars": 40},
            {"text": "tamanho grande", "kind": "output_min_chars", "min_chars": 5000},
            {"text": "regex ruim", "kind": "output_contains", "pattern": "("},
        ],
        RESULT,
    )
    assert verdicts["tem três pontos"].passed is True
    assert verdicts["menciona banco"].passed is False
    assert "não contém" in verdicts["menciona banco"].detail
    assert "summary/notes" in (verdicts["menciona banco"].required_change or "")
    assert verdicts["tamanho ok"].passed is True
    assert verdicts["tamanho grande"].passed is False
    assert "mínimo exigido 5000" in verdicts["tamanho grande"].detail
    assert verdicts["regex ruim"].passed is False


def test_output_criteria_work_without_workspace_runtime() -> None:
    # Sem workspace no resultado os critérios de arquivo são inverificáveis;
    # os de saída textual continuam decidíveis.
    verdicts = _verdicts(
        [
            {"text": "arquivo", "kind": "file_created", "path": "x.md"},
            {"text": "texto", "kind": "output_contains", "pattern": "PONTO"},
        ],
        RESULT,
    )
    assert verdicts["arquivo"].passed is None
    assert verdicts["texto"].passed is True


def test_output_criteria_parameters_and_labels() -> None:
    with pytest.raises(ValidationError):
        AcceptanceCriterion(text="x", kind=CriterionKind.OUTPUT_CONTAINS)
    with pytest.raises(ValidationError):
        AcceptanceCriterion(text="x", kind=CriterionKind.OUTPUT_MIN_CHARS)
    with pytest.raises(ValidationError):
        AcceptanceCriterion(text="x", kind=CriterionKind.OUTPUT_MIN_CHARS, min_chars=0)

    contains = AcceptanceCriterion(text="cita fonte", kind="output_contains", pattern="fonte")
    assert contains.label == "cita fonte [output_contains; pattern='fonte']"
    minimum = AcceptanceCriterion(text="denso", kind="output_min_chars", min_chars=300)
    assert minimum.label == "denso [output_min_chars; min_chars=300]"
    assert contains.kind.is_objective and minimum.kind.is_objective
