"""CLI contra uma API simulada."""

from __future__ import annotations

import json

import httpx

from app.cli import main


class _Api:
    def __init__(self) -> None:
        self.polls = 0
        self.decisions: list[str] = []
        self.created: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["x-api-key"] == "chave-teste"
        path = request.url.path
        if request.method == "POST" and path == "/workflows":
            self.created.append(json.loads(request.content.decode("utf-8")))
            return httpx.Response(202, json={"workflow_id": "wf-1", "status": "running", "current_stage": "queued"})
        if request.method == "GET" and path == "/workflows/wf-1":
            self.polls += 1
            if self.polls < 3:
                return httpx.Response(200, json={"workflow_id": "wf-1", "status": "running", "current_stage": "executing", "iteration": 0, "tasks": [{}], "usage": {"cost_usd": 0.01}, "pending_decision": None, "final_output": None})
            return httpx.Response(200, json={"workflow_id": "wf-1", "status": "completed", "current_stage": "completed", "iteration": 0, "tasks": [{}], "usage": {"cost_usd": 0.12}, "pending_decision": None, "final_output": "# Entrega\nfeito"})
        if request.method == "POST" and path == "/workflows/wf-1/decision":
            self.decisions.append(json.loads(request.content)["decision"])
            return httpx.Response(202, json={"workflow_id": "wf-1", "status": "resuming"})
        if request.method == "POST" and path == "/workflows/wf-1/cancel":
            return httpx.Response(202, json={"workflow_id": "wf-1", "status": "cancelled"})
        return httpx.Response(404, json={"detail": "Workflow não encontrado."})


def _run(api: _Api, *argv: str) -> int:
    return main(["--url", "http://api", "--api-key", "chave-teste", *argv], transport=httpx.MockTransport(api.handler))


def test_run_submits_waits_and_prints_delivery(capsys) -> None:
    api = _Api()
    code = _run(api, "run", "--project", "demo", "--request", "Crie testes para o módulo", "--criterion", "testes passam", "--budget-usd", "0.5", "--poll-seconds", "0")
    out, err = capsys.readouterr()
    assert code == 0
    assert api.created == [{"project_id": "demo", "request": "Crie testes para o módulo", "acceptance_criteria": ["testes passam"], "budget": {"max_cost_usd": 0.5}}]
    assert "# Entrega" in out and "feito" in out
    assert "workflow wf-1" in err and "completed" in err and "US$ 0.1200" in err


def test_run_no_wait_prints_only_the_id(capsys) -> None:
    api = _Api()
    assert _run(api, "run", "--project", "demo", "--request", "Pedido suficientemente longo", "--no-wait") == 0
    assert capsys.readouterr().out.strip() == "wf-1"


def test_status_json_decide_cancel_and_errors(capsys) -> None:
    api = _Api()
    api.polls = 10
    assert _run(api, "--json", "status", "wf-1") == 0
    assert json.loads(capsys.readouterr().out)["status"] == "completed"
    assert _run(api, "decide", "wf-1", "accept_partial") == 0 and api.decisions == ["accept_partial"]
    assert _run(api, "cancel", "wf-1") == 0
    assert _run(api, "status", "wf-404") == 2
    assert "erro 404" in capsys.readouterr().err
