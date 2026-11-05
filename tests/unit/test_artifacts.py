from pathlib import Path

from app.graph.state import WorkflowPhase, WorkflowState
from app.infrastructure.artifacts import persist_delivery
from app.models.task import AgentTask, Capability, TaskStatus


def test_persist_delivery_writes_markdown_and_published_files(tmp_path: Path):
    task = AgentTask(
        title="docs",
        description="escrever",
        capability=Capability.DOCUMENTATION,
        acceptance_criteria=["arquivo existe"],
        status=TaskStatus.COMPLETED,
        result={
            "workspace": {
                "published_files": [
                    {"path": "docs/proximos-passos.md", "content": "# Passos\n"}
                ]
            }
        },
    )
    state = WorkflowState(
        request="Crie docs/proximos-passos.md",
        project_id="demo",
        workflow_id="wf-local-1",
        owner_client_id="dev-client",
        plan=[task],
        phase=WorkflowPhase.COMPLETED,
        final_output="# Entrega — demo\n\nPronto.",
    )

    target = persist_delivery(state, tmp_path)

    assert target == tmp_path / "wf-local-1"
    assert (target / "ENTREGA.md").read_text(encoding="utf-8") == (
        "# Entrega — demo\n\nPronto.\n"
    )
    assert (target / "docs" / "proximos-passos.md").read_text(
        encoding="utf-8"
    ) == "# Passos\n"


def test_persist_delivery_rejects_path_escape(tmp_path: Path):
    task = AgentTask(
        title="docs",
        description="escrever",
        capability=Capability.DOCUMENTATION,
        acceptance_criteria=["ok"],
        status=TaskStatus.COMPLETED,
        result={
            "workspace": {
                "published_files": [
                    {"path": "../outside.md", "content": "nope"}
                ]
            }
        },
    )
    state = WorkflowState(
        request="escape",
        project_id="demo",
        workflow_id="wf-escape",
        owner_client_id="dev-client",
        plan=[task],
        phase=WorkflowPhase.COMPLETED,
        final_output="ok",
    )

    target = persist_delivery(state, tmp_path)

    assert target == tmp_path / "wf-escape"
    assert (target / "ENTREGA.md").exists()
    assert not (tmp_path / "outside.md").exists()
    assert not list(tmp_path.glob("**/outside.md"))
