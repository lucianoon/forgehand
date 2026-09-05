"""CLI do Forgehand: envia pedidos e acompanha uma API em execução.

Configuração: FORGEHAND_URL (http://localhost:8000) e FORGEHAND_API_KEY
(dev-key). Progresso vai para stderr; entrega ou --json vão para stdout.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import time
from typing import Any, Literal
from uuid import uuid4

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.factory.intake import (
    DirectWorkOrderInput,
    GitHubIssueWorkOrderInput,
    parse_github_issue_url,
)

SUCCESS = {"completed", "ready_for_human_review"}
TERMINAL = SUCCESS | {"failed", "cancelled"}
WAITING = {"awaiting_decision"}


def _nonblank(value: str) -> str:
    value = value.strip()
    if not value:
        raise argparse.ArgumentTypeError("informe um texto não vazio")
    return value


def _positive_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("informe um número finito maior que zero")
    return number


def _poll_seconds(value: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise argparse.ArgumentTypeError("informe um número finito maior ou igual a zero")
    return number


def _positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("informe um inteiro maior que zero")
    return number


def _tracking_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--timeout", type=_positive_float, default=900.0,
                        help="tempo de acompanhamento; expirar não cancela a execução")
    parser.add_argument("--poll-seconds", type=_poll_seconds, default=2.0)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="forgehand", description="Cliente de linha de comando do Forgehand")
    parser.add_argument("--url", default=os.getenv("FORGEHAND_URL", "http://localhost:8000"))
    parser.add_argument("--api-key", default=os.getenv("FORGEHAND_API_KEY", "dev-key"))
    parser.add_argument("--json", action="store_true", help="imprime o status completo em JSON")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="cria um workflow analítico e acompanha até o fim")
    run.add_argument("--project", required=True)
    run.add_argument("--request", required=True)
    run.add_argument("--criterion", action="append", default=[], help="critério de aceitação (repetível)")
    run.add_argument("--budget-usd", type=float, default=None)
    run.add_argument("--max-iterations", type=int, default=None)
    run.add_argument("--no-wait", action="store_true", help="só cria e imprime o id")
    _tracking_options(run)

    deliver = sub.add_parser("deliver", help="executa uma entrega por PR com CI e merge humano")
    deliver.add_argument("--project", required=True, type=_nonblank)
    source = deliver.add_mutually_exclusive_group(required=True)
    source.add_argument("--repository", type=_nonblank, help="repositório GitHub owner/repo")
    source.add_argument("--issue", type=_nonblank, help="URL HTTPS de uma issue GitHub")
    deliver.add_argument("--request", type=_nonblank, help="resultado desejado; obrigatório com --repository")
    deliver.add_argument("--criterion", action="append", required=True, type=_nonblank,
                         help="critério de aceitação (repetível)")
    deliver.add_argument("--budget-usd", required=True, type=_positive_float)
    deliver.add_argument("--base-ref", default="main", type=_nonblank)
    deliver.add_argument("--expected-base-sha", type=_nonblank, help="commit esperado; somente com --repository")
    deliver.add_argument("--build-profile", type=_nonblank)
    deliver.add_argument("--idempotency-key", type=_nonblank, help="reutilize a mesma chave para repetir o mesmo pedido")
    deliver.add_argument("--max-tokens", type=_positive_int, default=500_000)
    deliver.add_argument("--max-iterations", type=_positive_int, default=3)
    deliver.add_argument("--max-wall-clock-seconds", type=_positive_int, default=1800)
    deliver.add_argument("--no-wait", action="store_true", help="só cria e imprime o id")
    deliver.add_argument("--dry-run", action="store_true", help="valida e imprime o pedido JSON sem acessar a rede")
    _tracking_options(deliver)

    wait = sub.add_parser("wait", help="acompanha uma execução existente sem reenviar o pedido")
    wait.add_argument("workflow_id", type=_nonblank)
    _tracking_options(wait)

    doctor = sub.add_parser("doctor", help="diagnostica a instalação sem iniciar IA")
    doctor.add_argument("--json", dest="doctor_json", action="store_true",
                        help="imprime o diagnóstico validado em JSON")

    status = sub.add_parser("status", help="consulta um workflow")
    status.add_argument("workflow_id")
    decide = sub.add_parser("decide", help="responde ao gate humano")
    decide.add_argument("workflow_id")
    decide.add_argument("decision", choices=["retry", "accept_partial", "abort"])
    cancel = sub.add_parser("cancel", help="cancela um workflow")
    cancel.add_argument("workflow_id")
    return parser


def _delivery_body(args: argparse.Namespace) -> dict[str, Any]:
    if args.repository and not args.request:
        raise ValueError("--repository exige --request")
    if args.issue and (args.request is not None or args.expected_base_sha is not None):
        raise ValueError("--issue não aceita --request nem --expected-base-sha")
    ref = args.base_ref
    # Match the factory workspace's permitted ASCII ref alphabet before dispatch.
    if (re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", ref) is None or ref.endswith(("/", "."))
            or ".." in ref or "@{" in ref or "//" in ref
            or re.search(r"[\x00-\x20\x7f~^:?*\[\\]", ref)
            or any(part.startswith(".") or part.endswith(".lock") for part in ref.split("/"))):
        raise ValueError("--base-ref deve ser uma referência permitida pela fábrica")
    repository = args.repository
    if args.issue:
        repository, _, _ = parse_github_issue_url(args.issue, ["github.com"])
    if (not re.fullmatch(r"[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+", repository)
            or repository.split("/")[1] in {".", ".."}):
        raise ValueError("o destino deve usar owner/repo válido do GitHub")
    order: dict[str, Any] = {
        "base_ref": ref,
        "acceptance_criteria": args.criterion,
        "limits": {
            "max_tokens": args.max_tokens,
            "max_cost_usd": args.budget_usd,
            "max_iterations": args.max_iterations,
            "max_wall_clock_seconds": args.max_wall_clock_seconds,
        },
        "build_profile": args.build_profile,
        "idempotency_key": args.idempotency_key or str(uuid4()),
        "delivery_policy": {
            "create_pull_request": True, "wait_for_checks": True,
            "checks_timeout_seconds": 900, "require_human_merge": True,
        },
    }
    model: DirectWorkOrderInput | GitHubIssueWorkOrderInput
    if args.repository:
        model = DirectWorkOrderInput.model_validate({**order, "repository": args.repository,
            "requested_outcome": args.request, "expected_base_sha": args.expected_base_sha})
    else:
        model = GitHubIssueWorkOrderInput.model_validate({**order, "issue_url": args.issue})
    return {"project_id": args.project, "work_order": model.model_dump(mode="json", exclude_none=True)}


def _client(args: argparse.Namespace, transport: httpx.BaseTransport | None) -> httpx.Client:
    return httpx.Client(
        base_url=args.url,
        headers={"X-API-Key": args.api_key, "content-type": "application/json; charset=utf-8"},
        timeout=30, transport=transport,
    )


class _InvalidAPIResponse(ValueError):
    """The API did not return the expected contract; never include its body."""


class _ResponseModel(BaseModel):
    model_config = ConfigDict(strict=True, allow_inf_nan=False)


class _WorkflowReceipt(_ResponseModel):
    workflow_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,254}$")


class _PendingDecision(_ResponseModel):
    reason: str | None = None
    options: list[str] | None = None


class _DeliverySummary(_ResponseModel):
    url: str | None = None
    commit_sha: str | None = None
    ci_state: str | None = None


class _StatusPayload(_WorkflowReceipt):
    status: Literal["queued", "running", "completed", "ready_for_human_review",
                    "failed", "cancelled", "awaiting_decision"]
    current_stage: str | None = None
    iteration: int | None = Field(default=None, ge=0)
    usage: dict[str, float] | None = None
    tasks: list[dict[str, Any]] | None = None
    pending_decision: _PendingDecision | None = None
    delivery: _DeliverySummary | None = None
    final_output: str | None = None


class _InstallationCheck(_ResponseModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,79}$")
    status: Literal["pass", "fail", "warning"]
    code: str = Field(pattern=r"^[a-z][a-z0-9_]{0,119}$")
    detail: str | None = Field(default=None, max_length=1000)


class _InstallationWorkers(_ResponseModel):
    active: int = Field(ge=0)
    compatible: int = Field(ge=0)
    incompatible: int = Field(ge=0)
    legacy: int = Field(ge=0)


class _InstallationConfiguration(_ResponseModel):
    workspace_root: str | None = None
    workspace_identity: str | None = None
    build_profiles_digest: str | None = None
    source_digest: str | None = None
    dependency_network_enabled: bool = False
    approved_scm_hosts: list[str] = Field(default_factory=list)
    queue_backend: str
    checkpointer_backend: str
    command_backend: str
    docker_socket: str | None = None


class _InstallationJobs(_ResponseModel):
    incompatible: int = Field(ge=0)
    legacy_unbound: int = Field(ge=0)
    unconfigured: int = Field(ge=0)


class _InstallationReport(_ResponseModel):
    schema_version: Literal[1]
    ready: bool
    factory_mode: bool
    revision: str | None
    fingerprint: str | None
    checks: list[_InstallationCheck] = Field(min_length=1)
    workers: _InstallationWorkers
    jobs: _InstallationJobs
    expected_workers: int = Field(ge=1)
    configuration: _InstallationConfiguration


def _doctor(client: httpx.Client, as_json: bool) -> int:
    response = client.get("/operations/installation")
    if response.status_code not in {200, 503}:
        return _fail(response)
    try:
        report = _InstallationReport.model_validate(_response_object(response))
    except ValidationError:
        raise _InvalidAPIResponse() from None
    if (report.ready and any(check.status == "fail" for check in report.checks)) or (
        report.ready != (response.status_code == 200)
    ):
        raise _InvalidAPIResponse()
    if as_json:
        # Emit the validated contract only, never an arbitrary server response body.
        sys.stdout.write(report.model_dump_json(indent=2) + "\n")
    else:
        state = "pronta" if report.ready else "precisa de atenção"
        sys.stdout.write(f"Instalação {state}.\n")
        for check in report.checks:
            detail = f" — {check.detail}" if check.detail else ""
            sys.stdout.write(f"[{check.status}] {check.name}: {check.code}{detail}\n")
        workers = report.workers
        sys.stdout.write(
            f"Workers compatíveis: {workers.compatible}/{report.expected_workers} exigidos "
            f"({workers.active} ativos).\n"
        )
    return 0 if report.ready else 1


def _response_object(response: httpx.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError:
        raise _InvalidAPIResponse() from None
    if not isinstance(payload, dict):
        raise _InvalidAPIResponse()
    return payload


def _created_id(response: httpx.Response) -> str:
    try:
        return _WorkflowReceipt.model_validate(_response_object(response)).workflow_id
    except ValidationError:
        raise _InvalidAPIResponse() from None


def _status_payload(response: httpx.Response, workflow_id: str) -> dict[str, Any]:
    payload = _response_object(response)
    try:
        validated = _StatusPayload.model_validate(payload)
    except ValidationError:
        raise _InvalidAPIResponse() from None
    if validated.workflow_id != workflow_id:
        raise _InvalidAPIResponse()
    # Validate without replacing the original document returned by --json.
    return payload


def _fail(response: httpx.Response) -> int:
    messages = {
        400: "Pedido rejeitado pela API.", 401: "Autenticação recusada.",
        403: "Acesso ou papel insuficiente.", 404: "Recurso não encontrado.",
        409: "Conflito de estado ou configuração.", 422: "Pedido inválido para a API.",
        429: "Limite de requisições excedido.",
    }
    # Proxies and servers can return arbitrary HTML/JSON, including credentials.
    detail = messages.get(response.status_code, "A API não concluiu a solicitação.")
    sys.stderr.write(f"erro {response.status_code}: {detail}\n")
    return 2


def _uncertain_submission_hint() -> None:
    sys.stderr.write("O envio pode ter sido recebido. Consulte o histórico ou repita o mesmo pedido com a mesma --idempotency-key.\n")


def _print_status(payload: dict[str, Any], as_json: bool) -> None:
    if as_json:
        sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        return
    usage = payload.get("usage") or {}
    sys.stderr.write(
        f"[{payload.get('status')}] etapa={payload.get('current_stage')} iteração={payload.get('iteration')} "
        f"tarefas={len(payload.get('tasks') or [])} custo=US$ {float(usage.get('cost_usd') or 0):.4f}\n"
    )
    pending = payload.get("pending_decision")
    if pending:
        sys.stderr.write(f"gate humano: {pending.get('reason')} · opções: {', '.join(pending.get('options') or [])}\n")
    delivery = payload.get("delivery") or {}
    if delivery:
        sys.stdout.write(
            f"PR: {delivery.get('url') or 'não disponível'}\n"
            f"Commit: {delivery.get('commit_sha') or 'não disponível'}\n"
            f"CI: {delivery.get('ci_state') or 'não disponível'}\n"
        )
    if payload.get("status") == "ready_for_human_review":
        sys.stdout.write("Pronto para revisão; o merge é uma decisão humana.\n")
    if payload.get("final_output"):
        sys.stdout.write(str(payload["final_output"]) + "\n")


def _resume_hint(workflow_id: str) -> None:
    sys.stderr.write(f"A execução não foi cancelada. Retome com: forgehand wait {workflow_id}\n")


def _wait(client: httpx.Client, workflow_id: str, timeout: float, poll: float, as_json: bool) -> int:
    deadline = time.monotonic() + timeout
    last_stage = None
    while time.monotonic() < deadline:
        try:
            response = client.get(f"/workflows/{workflow_id}")
        except httpx.RequestError:
            sys.stderr.write(f"Falha de transporte ao consultar workflow {workflow_id}.\n")
            _resume_hint(workflow_id)
            return 2
        if response.status_code != 200:
            result = _fail(response)
            _resume_hint(workflow_id)
            return result
        try:
            payload = _status_payload(response, workflow_id)
        except _InvalidAPIResponse:
            sys.stderr.write("Resposta inválida da API ao consultar o workflow.\n")
            _resume_hint(workflow_id)
            return 2
        stage = (payload.get("status"), payload.get("current_stage"))
        if stage != last_stage and not as_json:
            sys.stderr.write(f"  {stage[0]} · {stage[1]}\n")
            last_stage = stage
        if payload.get("status") in TERMINAL or payload.get("status") in WAITING:
            _print_status(payload, as_json)
            return 0 if payload.get("status") in SUCCESS else 1
        time.sleep(min(poll, max(0, deadline - time.monotonic())))
    sys.stderr.write(f"timeout após {timeout:g}s aguardando {workflow_id}\n")
    _resume_hint(workflow_id)
    return 3


def _execute(args: argparse.Namespace, client: httpx.Client, body: dict[str, Any] | None) -> int:
    if args.command == "doctor":
        return _doctor(client, args.json or args.doctor_json)
    if args.command in {"run", "deliver"}:
        assert body is not None
        response = client.post("/workflows", content=json.dumps(body, ensure_ascii=False).encode("utf-8"))
        if response.status_code != 202:
            result = _fail(response)
            if args.command == "deliver" and (response.status_code == 408 or response.status_code >= 500):
                _uncertain_submission_hint()
            return result
        workflow_id = _created_id(response)
        sys.stderr.write(f"workflow {workflow_id}\n")
        if args.no_wait:
            sys.stdout.write(workflow_id + "\n")
            return 0
        return _wait(client, workflow_id, args.timeout, args.poll_seconds, args.json)
    if args.command == "wait":
        return _wait(client, args.workflow_id, args.timeout, args.poll_seconds, args.json)
    if args.command == "status":
        response = client.get(f"/workflows/{args.workflow_id}")
        if response.status_code != 200:
            return _fail(response)
        _print_status(_status_payload(response, args.workflow_id), args.json)
        return 0
    if args.command == "decide":
        response = client.post(f"/workflows/{args.workflow_id}/decision", json={"decision": args.decision})
        if response.status_code != 202:
            return _fail(response)
        sys.stderr.write(f"decisão '{args.decision}' enviada\n")
        return 0
    if args.command == "cancel":
        response = client.post(f"/workflows/{args.workflow_id}/cancel")
        if response.status_code != 202:
            return _fail(response)
        sys.stderr.write("cancelado\n")
        return 0
    return 2


def main(argv: list[str] | None = None, *, transport: httpx.BaseTransport | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    body: dict[str, Any] | None = None
    if args.command == "deliver":
        try:
            body = _delivery_body(args)
        except ValidationError as error:
            fields = ", ".join(".".join(str(part) for part in item["loc"]) for item in error.errors(include_input=False))
            parser.error(f"revise os campos da entrega: {fields}")
        except ValueError as error:
            parser.error(str(error))
        assert body is not None
        if args.dry_run:
            sys.stdout.write(json.dumps(body, ensure_ascii=False, indent=2) + "\n")
            return 0
        sys.stderr.write(f"Chave de idempotência: {body['work_order']['idempotency_key']}\n")
    elif args.command == "run":
        body = {"project_id": args.project, "request": args.request}
        if args.criterion:
            body["acceptance_criteria"] = args.criterion
        budget = {k: v for k, v in (("max_cost_usd", args.budget_usd), ("max_iterations", args.max_iterations)) if v is not None}
        if budget:
            body["budget"] = budget
    try:
        with _client(args, transport) as client:
            return _execute(args, client, body)
    except (httpx.RequestError, _InvalidAPIResponse) as error:
        message = "Resposta inválida da API." if isinstance(error, _InvalidAPIResponse) else "Falha de transporte ao acessar a API."
        sys.stderr.write(message + " Nenhum reenvio automático foi feito.\n")
        if args.command == "deliver":
            _uncertain_submission_hint()
        elif args.command == "status":
            _resume_hint(args.workflow_id)
        elif getattr(args, "workflow_id", None):
            sys.stderr.write(f"Workflow: {args.workflow_id}\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
