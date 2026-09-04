"""Grounding por relevância: sem palavra do pedido, o arquivo não entra (salvo
referências do projeto); o total de caracteres é limitado."""

from __future__ import annotations

from pathlib import Path

from app.infrastructure.repository_grounding import RepositoryGroundingCollector


def _repo(tmp_path: Path) -> Path:
    (tmp_path / "app").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "README.md").write_text("# Projeto\n\nServiço de pagamentos.\n", encoding="utf-8")
    (tmp_path / "app" / "billing.py").write_text(
        "def cobrar(valor):\n    return valor * 1.1\n", encoding="utf-8"
    )
    (tmp_path / "app" / "workflow_engine.py").write_text(
        "class Engine:\n    pass\n" * 30, encoding="utf-8"
    )
    (tmp_path / "app" / "graph_nodes.py").write_text("NODES = []\n" * 30, encoding="utf-8")
    (tmp_path / "tests" / "test_misc.py").write_text("def test_x():\n    assert True\n", encoding="utf-8")
    return tmp_path


def test_unrelated_files_are_omitted_but_project_references_stay(tmp_path: Path) -> None:
    grounding = RepositoryGroundingCollector(str(_repo(tmp_path)), max_files=16).collect(
        "corrigir a função cobrar em billing"
    )
    paths = [item["path"] for item in grounding["evidence"]]
    assert "app/billing.py" in paths
    assert "README.md" in paths  # referência do projeto, sem hit de palavra
    assert "app/workflow_engine.py" not in paths and "app/graph_nodes.py" not in paths
    assert grounding["omitted_candidates"] >= 2
    assert [item["id"] for item in grounding["evidence"]] == [f"E{i}" for i in range(1, len(paths) + 1)]


def test_legacy_behaviour_when_keyword_match_not_required(tmp_path: Path) -> None:
    grounding = RepositoryGroundingCollector(
        str(_repo(tmp_path)), max_files=16, require_keyword_match=False
    ).collect("corrigir a função cobrar em billing")
    paths = {item["path"] for item in grounding["evidence"]}
    assert {"app/billing.py", "app/workflow_engine.py", "app/graph_nodes.py"} <= paths


def test_total_chars_budget_caps_the_prefix(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    for i in range(6):
        (repo / "app" / f"billing_{i}.py").write_text(
            f"# billing módulo {i}\n" + f"x{i} = {i}\n" * 80, encoding="utf-8"
        )
    small = RepositoryGroundingCollector(
        str(repo), max_files=16, max_total_chars=1_200, require_keyword_match=True
    ).collect("billing")
    large = RepositoryGroundingCollector(
        str(repo), max_files=16, max_total_chars=200_000, require_keyword_match=True
    ).collect("billing")
    assert small["total_chars"] <= 1_200 + max(len(e["excerpt"]) for e in small["evidence"])
    assert len(small["evidence"]) >= 1
    assert len(small["evidence"]) < len(large["evidence"])
    assert small["omitted_candidates"] > large["omitted_candidates"]


def test_empty_request_still_returns_project_references(tmp_path: Path) -> None:
    grounding = RepositoryGroundingCollector(str(_repo(tmp_path))).collect("")
    assert [item["path"] for item in grounding["evidence"]] == ["README.md"]
