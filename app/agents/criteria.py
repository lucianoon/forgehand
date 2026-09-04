"""Avaliação determinística dos critérios objetivos.

O judge LLM só decide o que é subjetivo. Tudo que o workspace runtime, os
validadores e o grounding já sabem — arquivo criado, só criações, mudanças
restritas a paths, conteúdo presente, testes/lint/tipos verdes, citations
válidas — é decidido aqui, por código, e entra em `criteria_scores` como 1.0
ou 0.0. Um critério objetivo sem dado para decidir (sem workspace runtime,
sem o validador configurado, sem grounding) volta como `passed=None`: o judge
o entrega ao LLM com a nota de que não pôde ser verificado.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass
from typing import Any

from app.agents.grounding import (
    grounding_required,
    normalize_citations,
    validate_citations,
)
from app.agents.validation import ValidationSignal
from app.models.task import AcceptanceCriterion, AgentTask, CriterionKind

_SIGNAL_BY_KIND = {
    CriterionKind.TESTS_PASS: "pytest",
    CriterionKind.LINT_PASS: "ruff",
    CriterionKind.TYPES_PASS: "mypy",
}


@dataclass(frozen=True)
class ObjectiveVerdict:
    criterion: AcceptanceCriterion
    passed: bool | None  # None = não verificável com os dados disponíveis
    detail: str
    required_change: str | None = None

    @property
    def score(self) -> float:
        return 1.0 if self.passed else 0.0


def _workspace(task: AgentTask) -> dict[str, Any] | None:
    if not isinstance(task.result, dict):
        return None
    workspace = task.result.get("workspace")
    return workspace if isinstance(workspace, dict) else None


def _diffs(workspace: dict[str, Any]) -> list[dict[str, Any]]:
    diffs = workspace.get("file_diffs")
    return [d for d in diffs if isinstance(d, dict)] if isinstance(diffs, list) else []


def _changed_diffs(workspace: dict[str, Any]) -> list[dict[str, Any]]:
    return [d for d in _diffs(workspace) if d.get("changed") is True]


def _published_content(workspace: dict[str, Any], path: str) -> str | None:
    published = workspace.get("published_files")
    if not isinstance(published, list):
        return None
    for item in published:
        if isinstance(item, dict) and item.get("path") == path:
            content = item.get("content")
            return content if isinstance(content, str) else None
    return None


def no_existing_file_modified(task: AgentTask) -> bool | None:
    """Só criações (op=create em arquivos que não existiam). None sem
    workspace runtime — não há como saber o que mudou."""
    workspace = _workspace(task)
    if workspace is None or "file_diffs" not in workspace:
        return None
    changed = _changed_diffs(workspace)
    if not changed:
        return False
    return all(
        item.get("change_type") == "created"
        and item.get("operation", "create") == "create"
        for item in changed
    )


def evaluate_objective_criteria(
    task: AgentTask,
    context: dict[str, Any],
    signals: dict[str, ValidationSignal],
) -> list[ObjectiveVerdict]:
    return [
        _evaluate(criterion, task, context, signals)
        for criterion in task.acceptance_criteria
        if criterion.kind.is_objective
    ]


def _evaluate(
    criterion: AcceptanceCriterion,
    task: AgentTask,
    context: dict[str, Any],
    signals: dict[str, ValidationSignal],
) -> ObjectiveVerdict:
    kind = criterion.kind
    if kind in _SIGNAL_BY_KIND:
        return _signal_verdict(criterion, signals.get(_SIGNAL_BY_KIND[kind]))
    if kind is CriterionKind.CITATIONS_VALID:
        return _citations_verdict(criterion, task, context)
    if kind in (CriterionKind.OUTPUT_CONTAINS, CriterionKind.OUTPUT_MIN_CHARS):
        return _output_verdict(criterion, task)

    workspace = _workspace(task)
    if workspace is None or "file_diffs" not in workspace:
        return ObjectiveVerdict(
            criterion,
            None,
            "sem workspace runtime: não há registro do que a tarefa alterou.",
        )
    if kind is CriterionKind.FILE_CREATED:
        return _file_created(criterion, workspace)
    if kind is CriterionKind.FILE_MODIFIED:
        return _file_modified(criterion, workspace)
    if kind is CriterionKind.FILE_UNCHANGED:
        return _file_unchanged(criterion, workspace)
    if kind is CriterionKind.NO_EXISTING_FILE_MODIFIED:
        return _only_creations(criterion, task, workspace)
    if kind is CriterionKind.CHANGES_LIMITED_TO:
        return _changes_limited_to(criterion, workspace)
    if kind is CriterionKind.CONTENT_CONTAINS:
        return _content_contains(criterion, workspace)
    return ObjectiveVerdict(criterion, None, f"tipo {kind.value} sem avaliador.")


def _signal_verdict(
    criterion: AcceptanceCriterion, signal: ValidationSignal | None
) -> ObjectiveVerdict:
    name = _SIGNAL_BY_KIND[criterion.kind]
    if signal is None or signal.passed is None:
        return ObjectiveVerdict(
            criterion, None, f"sinal `{name}` indisponível (validador não configurado)."
        )
    if signal.passed:
        return ObjectiveVerdict(criterion, True, f"{name} passou.")
    detail = signal.details.strip() or f"exit_code={signal.exit_code}"
    return ObjectiveVerdict(
        criterion,
        False,
        f"{name} falhou: {detail[:400]}",
        required_change=f"Faça `{name}` passar: {detail[:200]}",
    )


def _citations_verdict(
    criterion: AcceptanceCriterion, task: AgentTask, context: dict[str, Any]
) -> ObjectiveVerdict:
    if not grounding_required(context):
        return ObjectiveVerdict(criterion, None, "sem grounding no contexto.")
    citations = normalize_citations(
        task.result.get("citations") if isinstance(task.result, dict) else None
    )
    errors = validate_citations(
        context, citations, allowed_ids=task.evidence_ids or None
    )
    if errors:
        return ObjectiveVerdict(
            criterion,
            False,
            "; ".join(errors),
            required_change="Inclua `citations` com evidence_ids reais e no escopo da tarefa.",
        )
    return ObjectiveVerdict(
        criterion,
        True,
        f"citations válidas: {', '.join(citations) or 'nenhuma exigida'}",
    )


def _file_created(
    criterion: AcceptanceCriterion, workspace: dict[str, Any]
) -> ObjectiveVerdict:
    path = criterion.path or ""
    entry = next((d for d in _diffs(workspace) if d.get("path") == path), None)
    if entry is None:
        return ObjectiveVerdict(
            criterion,
            False,
            f"`{path}` não foi tocado pela tarefa.",
            required_change=f"Crie o arquivo `{path}` (op=create).",
        )
    if entry.get("change_type") == "created":
        return ObjectiveVerdict(criterion, True, f"`{path}` criado.")
    return ObjectiveVerdict(
        criterion,
        False,
        f"`{path}` já existia ({entry.get('change_type')}), não foi criado.",
        required_change=f"`{path}` deveria ser um arquivo novo.",
    )


def _file_modified(
    criterion: AcceptanceCriterion, workspace: dict[str, Any]
) -> ObjectiveVerdict:
    path = criterion.path or ""
    entry = next((d for d in _diffs(workspace) if d.get("path") == path), None)
    if entry is not None and entry.get("change_type") == "modified":
        return ObjectiveVerdict(criterion, True, f"`{path}` alterado.")
    if entry is not None and entry.get("change_type") == "created":
        return ObjectiveVerdict(
            criterion,
            False,
            f"`{path}` foi criado, mas o critério exigia alterar o arquivo existente.",
            required_change=f"Altere `{path}` com op=replace em vez de recriá-lo.",
        )
    return ObjectiveVerdict(
        criterion,
        False,
        f"`{path}` não foi alterado pela tarefa.",
        required_change=f"Altere `{path}` (op=replace no trecho pertinente).",
    )


def _file_unchanged(
    criterion: AcceptanceCriterion, workspace: dict[str, Any]
) -> ObjectiveVerdict:
    """`path` não pode aparecer entre os diffs com mudança (alterado, criado
    por cima ou removido). Sem diff para o path = intocado."""
    path = criterion.path or ""
    entry = next((d for d in _changed_diffs(workspace) if d.get("path") == path), None)
    if entry is None:
        return ObjectiveVerdict(criterion, True, f"`{path}` não foi alterado.")
    return ObjectiveVerdict(
        criterion,
        False,
        f"`{path}` foi alterado ({entry.get('operation', entry.get('change_type'))}).",
        required_change=f"Não altere `{path}`; desfaça as mudanças nesse arquivo.",
    )


def _only_creations(
    criterion: AcceptanceCriterion, task: AgentTask, workspace: dict[str, Any]
) -> ObjectiveVerdict:
    result = no_existing_file_modified(task)
    if result is None:
        return ObjectiveVerdict(criterion, None, "sem registro de mudanças.")
    if result:
        return ObjectiveVerdict(criterion, True, "apenas arquivos novos foram criados.")
    touched = [
        f"{d.get('path')} ({d.get('operation', d.get('change_type'))})"
        for d in _changed_diffs(workspace)
        if not (
            d.get("change_type") == "created"
            and d.get("operation", "create") == "create"
        )
    ]
    detail = (
        "arquivos existentes alterados: " + ", ".join(touched)
        if touched
        else "nenhuma mudança registrada."
    )
    return ObjectiveVerdict(
        criterion,
        False,
        detail,
        required_change="Restrinja a mudança a arquivos novos; não altere existentes.",
    )


def _changes_limited_to(
    criterion: AcceptanceCriterion, workspace: dict[str, Any]
) -> ObjectiveVerdict:
    allowed = criterion.paths
    outside = [
        str(d.get("path"))
        for d in _changed_diffs(workspace)
        if not any(fnmatch.fnmatch(str(d.get("path")), glob) for glob in allowed)
    ]
    if outside:
        return ObjectiveVerdict(
            criterion,
            False,
            f"mudanças fora do escopo permitido ({', '.join(allowed)}): {', '.join(outside)}",
            required_change=f"Restrinja as mudanças a: {', '.join(allowed)}.",
        )
    return ObjectiveVerdict(
        criterion, True, f"mudanças restritas a {', '.join(allowed)}."
    )


def task_output_text(task: AgentTask) -> str:
    """Texto entregue por uma tarefa que não grava arquivo: summary + notes.
    Ignora exploração, citações e workspace — só o que o executor afirmou."""
    result = task.result
    if not isinstance(result, dict):
        return str(result or "")
    parts: list[str] = []
    summary = result.get("summary")
    if isinstance(summary, str):
        parts.append(summary)
    notes = result.get("notes")
    if isinstance(notes, list):
        parts.extend(note for note in notes if isinstance(note, str))
    return "\n".join(parts)


def _output_verdict(criterion: AcceptanceCriterion, task: AgentTask) -> ObjectiveVerdict:
    """Critérios sobre o texto entregue (tarefas sem arquivo): presença de
    conteúdo por regex e tamanho mínimo. Decididos por código, sem LLM."""
    text = task_output_text(task)
    if criterion.kind is CriterionKind.OUTPUT_MIN_CHARS:
        minimum = criterion.min_chars or 1
        length = len(text.strip())
        if length >= minimum:
            return ObjectiveVerdict(
                criterion, True, f"texto entregue com {length} caracteres (mínimo {minimum})."
            )
        return ObjectiveVerdict(
            criterion,
            False,
            f"texto entregue com {length} caracteres; mínimo exigido {minimum}.",
            required_change=(
                f"Entregue em summary/notes um texto com pelo menos {minimum} caracteres."
            ),
        )
    pattern = criterion.pattern or ""
    try:
        found = re.search(pattern, text, re.MULTILINE | re.IGNORECASE) is not None
    except re.error as exc:
        return ObjectiveVerdict(criterion, False, f"regex inválida no critério: {exc}")
    if found:
        return ObjectiveVerdict(criterion, True, f"texto entregue contém /{pattern}/.")
    return ObjectiveVerdict(
        criterion,
        False,
        f"texto entregue (summary/notes) não contém /{pattern}/.",
        required_change=(
            f"Inclua no texto entregue (summary/notes) o conteúdo exigido: /{pattern}/."
        ),
    )


def _content_contains(
    criterion: AcceptanceCriterion, workspace: dict[str, Any]
) -> ObjectiveVerdict:
    path = criterion.path or ""
    pattern = criterion.pattern or ""
    content = _published_content(workspace, path)
    if content is None:
        return ObjectiveVerdict(
            criterion,
            False,
            f"`{path}` não está entre os arquivos publicados pela tarefa.",
            required_change=f"Produza `{path}` contendo /{pattern}/.",
        )
    try:
        found = re.search(pattern, content, re.MULTILINE) is not None
    except re.error as exc:
        return ObjectiveVerdict(criterion, False, f"regex inválida no critério: {exc}")
    if found:
        return ObjectiveVerdict(criterion, True, f"`{path}` contém /{pattern}/.")
    return ObjectiveVerdict(
        criterion,
        False,
        f"`{path}` não contém /{pattern}/.",
        required_change=f"Inclua em `{path}` o conteúdo exigido: /{pattern}/.",
    )
