"""CLI do Forgehand: fala com uma API em execução.

    forgehand run --project demo --request "Crie testes para ..." --criterion "..."
    forgehand status <workflow_id>
    forgehand decide <workflow_id> retry|accept_partial|abort
    forgehand cancel <workflow_id>

Configuração por ambiente: FORGEHAND_URL (default http://localhost:8000) e
FORGEHAND_API_KEY (default dev-key). Saída humana em stderr, entrega em
stdout; `--json` imprime o status completo em JSON.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any

import httpx

TERMINAL = {"completed", "failed", "cancelled"}
WAITING = {"awaiting_decision"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="forgehand", description="Cliente de linha de comando do Forgehand")
    parser.add_argument("--url", default=os.getenv("FORGEHAND_URL", "http://localhost:8000"))
    parser.add_argument("--api-key", default=os.getenv("FORGEHAND_API_KEY", "dev-key"))
    parser.add_argument("--json", action="store_true", help="imprime o status completo em JSON")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="cria um workflow e acompanha até o fim")
    run.add_argument("--project", required=True)
    run.add_argument("--request", required=True)
    run.add_argument("--criterion", action="append", default=[], help="critério de aceitação (repetível)")
    run.add_argument("--budget-usd", type=float, default=None)
    run.add_argument("--max-iterations", type=int, default=None)
    run.add_argument("--no-wait", action="store_true", help="só cria e imprime o id")
    run.add_argument("--timeout", type=float, default=900.0)
    run.add_argument("--poll-seconds", type=float, default=2.0)

    status = sub.add_parser("status", help="consulta um workflow")
    status.add_argument("workflow_id")

    decide = sub.add_parser("decide", help="responde ao gate humano")
    decide.add_argument("workflow_id")
    decide.add_argument("decision", choices=["retry", "accept_partial", "abort"])

    cancel = sub.add_parser("cancel", help="cancela um workflow")
    cancel.add_argument("workflow_id")
    return parser


def _client(args: argparse.Namespace, transport: httpx.BaseTransport | None) -> httpx.Client:
    return httpx.Client(
        base_url=args.url,
        headers={"X-API-Key": args.api_key, "content-type": "application/json; charset=utf-8"},
        timeout=30,
        transport=transport,
    )


def _fail(response: httpx.Response) -> int:
    try:
        detail = response.json().get("detail", response.text)
    except ValueError:
        detail = response.text
    sys.stderr.write(f"erro {response.status_code}: {detail}\n")
    return 2


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
    if payload.get("final_output"):
        sys.stdout.write(str(payload["final_output"]) + "\n")


def _wait(client: httpx.Client, workflow_id: str, timeout: float, poll: float, as_json: bool) -> int:
    deadline = time.monotonic() + timeout
    last_stage = None
    while time.monotonic() < deadline:
        response = client.get(f"/workflows/{workflow_id}")
        if response.status_code != 200:
            return _fail(response)
        payload = response.json()
        stage = (payload.get("status"), payload.get("current_stage"))
        if stage != last_stage and not as_json:
            sys.stderr.write(f"  {stage[0]} · {stage[1]}\n")
            last_stage = stage
        if payload.get("status") in TERMINAL or payload.get("status") in WAITING:
            _print_status(payload, as_json)
            return 0 if payload.get("status") == "completed" else 1
        time.sleep(poll)
    sys.stderr.write(f"timeout após {timeout:g}s aguardando {workflow_id}\n")
    return 3


def main(argv: list[str] | None = None, *, transport: httpx.BaseTransport | None = None) -> int:
    args = build_parser().parse_args(argv)
    with _client(args, transport) as client:
        if args.command == "run":
            body: dict[str, Any] = {"project_id": args.project, "request": args.request}
            if args.criterion:
                body["acceptance_criteria"] = args.criterion
            budget = {k: v for k, v in (("max_cost_usd", args.budget_usd), ("max_iterations", args.max_iterations)) if v is not None}
            if budget:
                body["budget"] = budget
            response = client.post("/workflows", content=json.dumps(body, ensure_ascii=False).encode("utf-8"))
            if response.status_code != 202:
                return _fail(response)
            workflow_id = response.json()["workflow_id"]
            sys.stderr.write(f"workflow {workflow_id}\n")
            if args.no_wait:
                sys.stdout.write(workflow_id + "\n")
                return 0
            return _wait(client, workflow_id, args.timeout, args.poll_seconds, args.json)
        if args.command == "status":
            response = client.get(f"/workflows/{args.workflow_id}")
            if response.status_code != 200:
                return _fail(response)
            _print_status(response.json(), args.json)
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


if __name__ == "__main__":
    raise SystemExit(main())
