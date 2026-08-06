"""Fatos que o sistema verifica sem consultar o LLM.

O judge combina a opinião do LLM com sinais objetivos. Nem todo sinal objetivo
vem de ferramenta externa (`pytest`, `ruff`, `mypy`): parte vem de dados que o
próprio runtime já produziu — o resultado do validador de citations e o diff
efetivamente aplicado no workspace.

O LLM às vezes contradiz esses dados. Afirma que as citations são inválidas
depois de o validador determinístico ter confirmado que existem e estão no
escopo, ou que um arquivo pré-existente foi modificado quando o diff registra
apenas criação. Reconhecer essas contradições procurando substrings no texto do
LLM acopla o judge à redação dos critérios e ao idioma da resposta.

Aqui cada fato ganha um id estável. O judge injeta os ids no prompt, o schema
de saída exige que o LLM marque qual fato um critério ou uma observação invoca,
e a reconciliação acontece sobre esses ids. Nenhum texto livre é interpretado.

O fato é autoritativo nos DOIS sentidos: quando se confirma, sobrepõe a
rejeição do LLM; quando não se confirma, sobrepõe a aprovação. E o fato nunca
cria exigência por conta própria — ele só decide os itens que o LLM marcou.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from app.agents.grounding import grounding_required
from app.models.task import AgentTask

CITATIONS_VALID = "citations_valid"
ONLY_NEW_FILES = "only_new_files"


class DeterministicCheck(BaseModel):
    """Um fato verificado pelo runtime, com id estável para reconciliação."""

    id: str
    holds: bool
    statement: str


def _changed_diffs(task: AgentTask) -> list[dict[str, Any]] | None:
    """Diffs com alteração real na tentativa. None quando não há workspace."""
    if not isinstance(task.result, dict):
        return None
    workspace = task.result.get("workspace")
    if not isinstance(workspace, dict):
        return None
    file_diffs = workspace.get("file_diffs")
    if not isinstance(file_diffs, list) or not file_diffs:
        return None
    changed = [
        item
        for item in file_diffs
        if isinstance(item, dict) and item.get("changed") is True
    ]
    return changed or None


def citations_check(
    context: dict[str, Any], *, citations_are_valid: bool
) -> DeterministicCheck | None:
    """Só existe em modo grounded — sem grounding não há o que verificar."""
    if not grounding_required(context):
        return None
    return DeterministicCheck(
        id=CITATIONS_VALID,
        holds=citations_are_valid,
        statement=(
            "Todas as `citations` do resultado referenciam evidence_ids que existem "
            "no contexto e estão dentro do escopo atribuído à tarefa."
        ),
    )


def only_new_files_check(task: AgentTask) -> DeterministicCheck | None:
    """Só existe quando a tentativa alterou arquivos no workspace."""
    changed = _changed_diffs(task)
    if changed is None:
        return None
    return DeterministicCheck(
        id=ONLY_NEW_FILES,
        holds=all(item.get("change_type") == "created" for item in changed),
        statement=(
            "Todos os arquivos alterados nesta tentativa foram criados do zero; "
            "nenhum arquivo pré-existente foi modificado."
        ),
    )


def active_checks(
    task: AgentTask,
    context: dict[str, Any],
    *,
    citations_are_valid: bool,
) -> list[DeterministicCheck]:
    """Fatos aplicáveis a esta tarefa. Um fato inaplicável não entra no prompt."""
    candidates = (
        citations_check(context, citations_are_valid=citations_are_valid),
        only_new_files_check(task),
    )
    return [check for check in candidates if check is not None]


def format_checks_block(checks: list[DeterministicCheck]) -> str:
    if not checks:
        return ""
    lines = [
        "Fatos já verificados deterministicamente pelo sistema. São "
        "autoritativos — NÃO os contradiga:",
    ]
    for check in checks:
        verdict = "VERDADEIRO" if check.holds else "FALSO"
        lines.append(f"- [{check.id}] {verdict} — {check.statement}")
    lines.append(
        "\nSempre que um critério de aceitação, uma falha ou uma correção exigida "
        "se referir a um destes fatos, preencha `deterministic_check` com o id "
        "correspondente. Nesses itens o sistema usa o veredito verificado acima "
        "no lugar da sua avaliação. Deixe `deterministic_check` nulo quando o "
        "item não se referir a nenhum fato da lista."
    )
    return "\n".join(lines)
