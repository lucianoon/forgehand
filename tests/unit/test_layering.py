"""As camadas viraram regra verificável, não convenção em docstring.

Os docstrings de `app/graph/nodes.py` e `app/agents/executor.py` sempre
afirmaram uma direção de dependência que o código não sustentava: o judge
importava DTOs de `app.graph.nodes` e `app.infrastructure.settings` importava
`ExecutionStrategy` de `app.agents.executor`. Ambas apontavam para cima.

Este teste falha na próxima vez que isso acontecer.

Não é um mapa completo das camadas: `graph` e `infrastructure` são mutuamente
acopladas de propósito (o grafo usa tracing; a persistência serializa
`WorkflowState`), e `api` é o composition root e enxerga tudo. As regras abaixo
cobrem só as direções que precisam permanecer proibidas.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

APP_ROOT = Path(__file__).resolve().parents[2] / "app"

# camada -> pacotes de `app/` que ela NÃO pode importar
FORBIDDEN: dict[str, frozenset[str]] = {
    # Contratos compartilhados: folha absoluta. Qualquer import daqui para
    # cima recria o ciclo que a extração de app/models/contracts.py desfez.
    "models": frozenset({"agents", "api", "graph", "infrastructure", "providers"}),
    # Porta única para LLMs: não conhece quem a usa.
    "providers": frozenset({"agents", "api", "graph", "infrastructure"}),
    # Agentes falam com providers e models; a orquestração é que os injeta.
    "agents": frozenset({"api", "graph"}),
    # Infra não conhece agente nem composition root.
    "infrastructure": frozenset({"agents", "api"}),
}


def _imported_app_packages(module_path: Path) -> set[str]:
    """Pacotes de primeiro nível sob `app/` que este módulo importa."""
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        names: list[str] = []
        if isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
        elif isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        for name in names:
            parts = name.split(".")
            if len(parts) > 2 and parts[0] == "app":
                imported.add(parts[1])
    return imported


def _modules_in(layer: str) -> list[Path]:
    return sorted(p for p in (APP_ROOT / layer).rglob("*.py"))


@pytest.mark.parametrize("layer", sorted(FORBIDDEN))
def test_layer_does_not_depend_upward(layer: str):
    forbidden = FORBIDDEN[layer]
    violations: list[str] = []
    for module_path in _modules_in(layer):
        for package in sorted(_imported_app_packages(module_path) & forbidden):
            relative = module_path.relative_to(APP_ROOT.parent)
            violations.append(f"{relative} importa app.{package}")

    assert violations == [], (
        f"camada `{layer}` não pode importar {sorted(forbidden)}: "
        + "; ".join(violations)
    )


def test_layering_rules_cover_existing_layers():
    """Guarda contra a regra silenciosamente virar no-op se um pacote sumir."""
    for layer in FORBIDDEN:
        assert (APP_ROOT / layer).is_dir(), f"camada `{layer}` não existe mais"
        assert _modules_in(layer), f"camada `{layer}` está vazia"
