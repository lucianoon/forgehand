"""Entrega final legível e workspace do executor isolado do servidor.

Achados da rodada real de 03/09/2026: o README gerado saiu embutido num repr
de dict Python, e o executor explorou o repositório do próprio Forgehand.
"""

from pathlib import Path

from app.api.container import ensure_executor_workspace
from app.graph.build_evidence import render_task_result
from app.infrastructure.settings import Settings


def test_render_task_result_uses_summary_notes_and_files() -> None:
    result = {
        "summary": "Redigi o README.",
        "operations": [],
        "notes": ["# Projeto\n\nConteúdo do README."],
        "citations": ["E5"],
        "exploration": {"tool_calls": 2, "trace": [{"name": "list_directory"}]},
        "workspace": {
            "applied_files": ["README.md"],
            "published_files": [{"path": "README.md", "content": "..."}],
            "deleted_paths": ["OLD.md"],
        },
    }
    text = render_task_result(result)
    assert text.startswith("Redigi o README.")
    assert "# Projeto\n\nConteúdo do README." in text
    assert "- `README.md`" in text and text.count("README.md") == 1
    assert "- `OLD.md` (removido)" in text
    assert "{'summary'" not in text and "exploration" not in text and "E5" not in text


def test_render_task_result_falls_back_without_contract() -> None:
    assert render_task_result(None) == ""
    assert render_task_result("texto puro") == "texto puro"
    rendered = render_task_result({"answer": 42, "exploration": {"rounds": 1}})
    assert '"answer": 42' in rendered and "exploration" not in rendered


def test_executor_workspace_default_is_not_the_server_directory() -> None:
    settings = Settings(_env_file=None)
    root = Path(settings.executor_workspace_root)
    assert root != Path(".")
    assert root.resolve() != Path.cwd().resolve()


def test_ensure_executor_workspace_creates_dedicated_directory(tmp_path) -> None:
    target = tmp_path / "nested" / "workspace"
    settings = Settings(_env_file=None, executor_workspace_root=str(target))
    created = ensure_executor_workspace(settings)
    assert created == target.resolve() and target.is_dir()
    # idempotente
    assert ensure_executor_workspace(settings) == created
