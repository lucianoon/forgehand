import asyncio
import sys

sys.path.insert(0, ".")
import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command
from app.graph.workflow import build_workflow
from app.models.task import AgentTask, Capability, EvaluationResult, TaskStatus


# ---- Fakes -------------------------------------------------------------
class FakePlanner:
    async def create_plan(self, request, context):
        t1 = AgentTask(
            title="backend",
            description="CRUD clientes",
            capability=Capability.BACKEND,
            acceptance_criteria=["CRUD ok"],
        )
        t2 = AgentTask(
            title="docs",
            description="README",
            capability=Capability.DOCUMENTATION,
            acceptance_criteria=["README ok"],
        )
        t3 = AgentTask(
            title="testes",
            description="pytest",
            capability=Capability.TESTING,
            acceptance_criteria=["cobre CRUD"],
            dependencies=[t1.id],
        )
        return [t1, t2, t3]


class FakeExecutor:
    async def execute(self, task, context):
        return {
            "result": {"code": f"# {task.title}"},
            "agent": "fake",
            "model": "fake-1",
            "tokens": 100,
            "cost_usd": 0.01,
        }


class SlowExecutor:
    async def execute(self, task, context):
        await asyncio.sleep(10)  # vai estourar o timeout


class FakeRegistry:
    def __init__(self, executor):
        self.executor = executor

    def select(self, task):
        return self.executor


class ApprovingJudge:
    async def evaluate(self, task, context):
        return EvaluationResult(
            task_id=task.id,
            approved=True,
            score=0.9,
            criteria_scores={c.text: 0.9 for c in task.acceptance_criteria},
            tests_passed=True,
        )


class RejectingJudge:
    """Rejeita 'backend' sempre -> forca escalation -> human_gate."""

    async def evaluate(self, task, context):
        if task.title == "backend":
            return EvaluationResult(
                task_id=task.id,
                approved=False,
                score=0.3,
                criteria_scores={},
                failures=["CRUD incompleto"],
                required_changes=["adicionar DELETE"],
            )
        return EvaluationResult(
            task_id=task.id, approved=True, score=0.9, criteria_scores={}
        )


class FakeMemory:
    def __init__(self):
        self.persisted = None

    async def load_context(self, project_id, request=""):
        return {"project": project_id}

    async def persist(self, state):
        self.persisted = state


def initial(wid):
    return {
        "request": "Crie API de clientes",
        "project_id": "demo",
        "workflow_id": wid,
        "owner_client_id": "client-demo",
    }


@pytest.mark.asyncio
async def test_workflow_smoke():
    # ---- 1. Caminho feliz: fan-out paralelo + deps + sintese ----------
    mem = FakeMemory()
    from app.graph.workflow import build_serde

    app = build_workflow(
        FakePlanner(),
        FakeRegistry(FakeExecutor()),
        ApprovingJudge(),
        mem,
        MemorySaver(serde=build_serde()),
    )
    cfg = {"configurable": {"thread_id": "t1"}}
    out = await app.ainvoke(initial("w1"), cfg)
    assert out["phase"].value == "completed", out["phase"]
    assert all(t.status == TaskStatus.COMPLETED for t in out["plan"])
    assert out["usage"]["tokens"] == 300 and abs(out["usage"]["cost_usd"] - 0.03) < 1e-9
    assert "backend" in out["final_output"] and mem.persisted is not None
    print("1. happy path OK — 3 tarefas, deps respeitadas, usage agregado")

    # ---- 2. Timeout vira FAILED, nao excecao --------------------------
    app2 = build_workflow(
        FakePlanner(),
        FakeRegistry(SlowExecutor()),
        ApprovingJudge(),
        FakeMemory(),
        MemorySaver(serde=build_serde()),
    )

    # timeout curto: patch nas tarefas via planner custom
    class ShortTimeoutPlanner(FakePlanner):
        async def create_plan(self, request, context):
            plan = await super().create_plan(request, context)
            return [
                t.model_copy(update={"timeout_seconds": 1, "max_attempts": 1})
                for t in plan
            ]

    app2 = build_workflow(
        ShortTimeoutPlanner(),
        FakeRegistry(SlowExecutor()),
        ApprovingJudge(),
        FakeMemory(),
        MemorySaver(serde=build_serde()),
    )
    cfg2 = {"configurable": {"thread_id": "t2"}}
    out2 = await app2.ainvoke(initial("w2"), cfg2)
    # max_attempts=1 -> ESCALATED -> human_gate -> interrupt
    assert "__interrupt__" in out2, out2.keys()
    payload = out2["__interrupt__"][0].value
    assert payload["reason"] == "tasks_escalated"
    assert any(
        "timeout" in (t["last_failure"] or "") for t in payload["escalated_tasks"]
    )
    print("2. timeout OK — vira ESCALATED e pausa no human_gate:", payload["reason"])

    # retomada com decisao humana: aceitar parcial
    out2b = await app2.ainvoke(Command(resume="accept_partial"), cfg2)
    assert out2b["phase"].value == "completed"
    assert "PARCIAL" in out2b["final_output"]
    print("3. resume OK — accept_partial gera entrega parcial explicita")

    # ---- 3. Rejeicao do judge -> replan -> escalation -> abort --------
    class TwoAttemptPlanner(FakePlanner):
        async def create_plan(self, request, context):
            plan = await super().create_plan(request, context)
            return [t.model_copy(update={"max_attempts": 2}) for t in plan]

    app3 = build_workflow(
        TwoAttemptPlanner(),
        FakeRegistry(FakeExecutor()),
        RejectingJudge(),
        FakeMemory(),
        MemorySaver(serde=build_serde()),
    )
    cfg3 = {"configurable": {"thread_id": "t3"}}
    out3 = await app3.ainvoke(initial("w3"), cfg3)
    assert "__interrupt__" in out3
    # replan anexou required_changes na descricao antes de re-executar
    state3 = await app3.aget_state(cfg3)
    backend = next(t for t in state3.values["plan"] if t.title == "backend")
    assert "adicionar DELETE" in backend.description
    assert backend.status == TaskStatus.ESCALATED and backend.attempt_count == 2
    docs = next(t for t in state3.values["plan"] if t.title == "docs")
    assert docs.status == TaskStatus.COMPLETED and docs.attempt_count == 1, (
        "COMPLETED nao pode re-executar no replan"
    )
    print(
        "4. replan OK — rejeitada re-executa com feedback do judge; COMPLETED intocada"
    )

    out3b = await app3.ainvoke(Command(resume="abort"), cfg3)
    assert out3b["phase"].value == "failed"
    assert "abortado" in out3b["final_output"]
    print("5. abort OK — decisao humana encerra com phase=failed")

    print("\n5/5 cenarios de integracao OK")
