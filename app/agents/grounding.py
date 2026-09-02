from __future__ import annotations

from typing import Any


def get_repository_grounding(context: dict[str, Any]) -> dict[str, Any] | None:
    grounding = context.get("repository_grounding")
    if not isinstance(grounding, dict):
        return None
    evidence = grounding.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        return None
    return grounding


def get_evidence_index(context: dict[str, Any]) -> dict[str, dict[str, Any]]:
    grounding = get_repository_grounding(context)
    if grounding is None:
        return {}
    index: dict[str, dict[str, Any]] = {}
    for item in grounding.get("evidence", []):
        if isinstance(item, dict) and isinstance(item.get("id"), str):
            index[item["id"]] = item
    return index


def grounding_required(context: dict[str, Any]) -> bool:
    grounding = get_repository_grounding(context)
    return bool(grounding and grounding.get("require_citations", True))


def normalize_citations(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    citations: list[str] = []
    for item in raw:
        if isinstance(item, str) and item not in citations:
            citations.append(item)
    return citations


def validate_citations(
    context: dict[str, Any],
    citations: list[str],
    *,
    allowed_ids: list[str] | None = None,
) -> list[str]:
    if not grounding_required(context):
        return []

    errors: list[str] = []
    evidence_index = get_evidence_index(context)
    if not citations:
        errors.append(
            "Resultado sem `citations`: cada afirmação sobre o repositório deve citar evidence_ids reais."
        )
        return errors

    unknown = [citation for citation in citations if citation not in evidence_index]
    if unknown:
        errors.append(
            "Resultado cita evidence_ids inexistentes no contexto: "
            + ", ".join(sorted(unknown))
        )

    if allowed_ids:
        allowed = set(allowed_ids)
        out_of_scope = [citation for citation in citations if citation not in allowed]
        if out_of_scope:
            errors.append(
                "Resultado cita evidências fora do escopo da tarefa: "
                + ", ".join(sorted(out_of_scope))
            )
    return errors


def build_grounding_prefix(context: dict[str, Any]) -> str | None:
    """Bloco de grounding COMPLETO e determinístico do workflow — o mesmo texto
    para planner, executor e judge, para servir de prefixo cacheável no
    provider. A seleção por tarefa (evidence_ids) vai no user content via
    format_evidence_focus, não aqui: qualquer variação quebra o cache."""
    block = format_repository_grounding(context, max_items=None)
    return block or None


def format_evidence_focus(evidence_ids: list[str] | None) -> str:
    """Aponta, dentro do grounding completo, quais evidências sustentam esta
    tarefa. Substitui o recorte por tarefa que antes ia no user content."""
    ids = [item for item in (evidence_ids or []) if isinstance(item, str)]
    if not ids:
        return ""
    return (
        "Evidências atribuídas a esta tarefa (fonte primária; as demais do "
        "grounding são contexto): " + ", ".join(f"[{item}]" for item in ids)
    )


def format_repository_grounding(
    context: dict[str, Any],
    *,
    evidence_ids: list[str] | None = None,
    max_items: int | None = 8,
) -> str:
    grounding = get_repository_grounding(context)
    if grounding is None:
        return ""

    evidence_index = get_evidence_index(context)
    selected_ids = evidence_ids or list(evidence_index)
    items = [
        evidence_index[evidence_id]
        for evidence_id in selected_ids
        if evidence_id in evidence_index
    ][:max_items]
    if not items:
        items = list(evidence_index.values())[:max_items]

    top_level = grounding.get("top_level_entries") or []
    parts = [
        "Grounding obrigatório do repositório:",
        f"- repo_root: {grounding.get('repo_root')}",
    ]
    if top_level:
        parts.append(
            "- top_level_entries: " + ", ".join(str(item) for item in top_level)
        )

    parts.append("- evidências disponíveis:")
    for item in items:
        parts.append(
            f"  [{item['id']}] {item['path']}:{item['line_start']}-{item['line_end']}"
        )
        parts.append(item["excerpt"])

    return "\n".join(parts)
