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

    code = sub.add_parser(
        "code",
        help="executa uma tarefa de código direto do terminal (estilo Claude Code / Codex)",
    )
    code.add_argument("prompt", help="instrução de codificação ou análise")
    code.add_argument("--project", default=os.getenv("FORGEHAND_PROJECT", "default"))
    code.add_argument("--criterion", action="append", default=[], help="critério de aceitação (repetível)")
    code.add_argument("--budget-usd", type=float, default=None)
    code.add_argument("--show-diff", action="store_true", help="exibe os diffs aplicados ao final")
    code.add_argument(
        "--auto-approve",
        action="store_true",
        help="aprova automaticamente (accept_partial) caso pare em gate humano",
    )
    code.add_argument("--timeout", type=float, default=900.0)
    code.add_argument("--poll-seconds", type=float, default=1.0)

    chat = sub.add_parser(
        "chat",
        help="inicia sessão interativa multi-turno no terminal (REPL estilo Claude Code)",
    )
    chat.add_argument("--project", default=os.getenv("FORGEHAND_PROJECT", "default"))
    chat.add_argument("--show-diff", action="store_true", help="exibe diffs a cada turno")
    chat.add_argument("--timeout", type=float, default=900.0)
    chat.add_argument("--poll-seconds", type=float, default=1.0)
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


def _extract_applied_diffs(payload: dict[str, Any]) -> tuple[list[str], list[dict[str, Any]]]:
    applied: list[str] = []
    diffs: list[dict[str, Any]] = []
    for task in payload.get("tasks") or []:
        if not isinstance(task, dict):
            continue
        result = task.get("result")
        if not isinstance(result, dict):
            continue
        workspace = result.get("workspace")
        if not isinstance(workspace, dict):
            continue
        for path in workspace.get("applied_files") or []:
            if isinstance(path, str) and path not in applied:
                applied.append(path)
        for diff in workspace.get("file_diffs") or []:
            if isinstance(diff, dict):
                diffs.append(diff)
    return applied, diffs


def _print_diffs(payload: dict[str, Any]) -> None:
    applied, diffs = _extract_applied_diffs(payload)
    if applied:
        sys.stderr.write(f"arquivos modificados ({len(applied)}): {', '.join(applied)}\n")
    for diff in diffs:
        diff_text = str(diff.get("diff") or "").strip()
        if diff_text:
            sys.stdout.write(diff_text + "\n")


def _wait_with_payload(
    client: httpx.Client,
    workflow_id: str,
    timeout: float,
    poll: float,
    as_json: bool,
) -> tuple[int, dict[str, Any] | None]:
    deadline = time.monotonic() + timeout
    last_stage = None
    while time.monotonic() < deadline:
        response = client.get(f"/workflows/{workflow_id}")
        if response.status_code != 200:
            return _fail(response), None
        payload = response.json()
        stage = (payload.get("status"), payload.get("current_stage"))
        if stage != last_stage and not as_json:
            sys.stderr.write(f"  {stage[0]} · {stage[1]}\n")
            last_stage = stage
        if payload.get("status") in TERMINAL or payload.get("status") in WAITING:
            _print_status(payload, as_json)
            return (0 if payload.get("status") == "completed" else 1), payload
        time.sleep(poll)
    sys.stderr.write(f"timeout após {timeout:g}s aguardando {workflow_id}\n")
    return 3, None


def _wait(client: httpx.Client, workflow_id: str, timeout: float, poll: float, as_json: bool) -> int:
    code, _ = _wait_with_payload(client, workflow_id, timeout, poll, as_json)
    return code


def _run_code(client: httpx.Client, args: argparse.Namespace) -> int:
    body: dict[str, Any] = {"project_id": args.project, "request": args.prompt}
    if args.criterion:
        body["acceptance_criteria"] = args.criterion
    if args.budget_usd is not None:
        body["budget"] = {"max_cost_usd": args.budget_usd}
    response = client.post("/workflows", content=json.dumps(body, ensure_ascii=False).encode("utf-8"))
    if response.status_code != 202:
        return _fail(response)
    workflow_id = response.json()["workflow_id"]
    sys.stderr.write(f"workflow {workflow_id}\n")
    code, payload = _wait_with_payload(
        client, workflow_id, args.timeout, args.poll_seconds, args.json
    )
    if (
        payload is not None
        and payload.get("status") in WAITING
        and args.auto_approve
    ):
        sys.stderr.write("auto-approve: enviando 'accept_partial'...\n")
        dec = client.post(
            f"/workflows/{workflow_id}/decision", json={"decision": "accept_partial"}
        )
        if dec.status_code != 202:
            return _fail(dec)
        code, payload = _wait_with_payload(
            client, workflow_id, args.timeout, args.poll_seconds, args.json
        )
    if payload is not None and args.show_diff and not args.json:
        _print_diffs(payload)
    return code


def _run_chat(client: httpx.Client, args: argparse.Namespace) -> int:
    sys.stderr.write(
        f"Forgehand Chat (projeto={args.project}) — digite sua instrução ou "
        "/diff, /status, /approve, /retry, /abort, /clear, /exit\n"
    )
    history: list[str] = []
    last_workflow_id: str | None = None
    last_payload: dict[str, Any] | None = None
    while True:
        sys.stderr.write("forgehand> ")
        sys.stderr.flush()
        line = sys.stdin.readline()
        if not line:
            break
        prompt = line.strip()
        if not prompt:
            continue
        if prompt in {"/exit", "/quit"}:
            break
        if prompt.lower() in {
            "oi",
            "olá",
            "ola",
            "hi",
            "hello",
            "hey",
            "bom dia",
            "boa tarde",
            "boa noite",
            "/help",
            "help",
            "ajuda",
        }:
            sys.stdout.write(
                "Olá! Sou o Forgehand. Posso explorar o repositório, criar/editar "
                "arquivos, rodar pytest/ruff/mypy e corrigir erros automaticamente.\n"
                "Exemplos:\n"
                "  - Explique a arquitetura deste projeto\n"
                "  - Adicione uma função em src/calculator.py e crie os testes\n"
                "Comandos da sessão: /diff, /status, /approve, /retry, /abort, /clear, /exit\n"
            )
            continue
        if prompt == "/clear":
            history.clear()
            sys.stderr.write("histórico de sessão limpo\n")
            continue
        if prompt == "/status":
            if not last_workflow_id:
                sys.stderr.write("nenhum workflow ativo na sessão\n")
                continue
            resp = client.get(f"/workflows/{last_workflow_id}")
            if resp.status_code == 200:
                last_payload = resp.json()
                _print_status(last_payload, args.json)
            else:
                _fail(resp)
            continue
        if prompt == "/diff":
            if last_payload is None:
                sys.stderr.write("nenhum resultado disponível para exibir diff\n")
            else:
                _print_diffs(last_payload)
            continue
        if prompt in {"/approve", "/retry", "/abort"}:
            if not last_workflow_id:
                sys.stderr.write("nenhum workflow pendente na sessão\n")
                continue
            decision_map = {
                "/approve": "accept_partial",
                "/retry": "retry",
                "/abort": "abort",
            }
            decision = decision_map[prompt]
            dec = client.post(
                f"/workflows/{last_workflow_id}/decision",
                json={"decision": decision},
            )
            if dec.status_code != 202:
                _fail(dec)
                continue
            _, last_payload = _wait_with_payload(
                client, last_workflow_id, args.timeout, args.poll_seconds, args.json
            )
            if last_payload is not None and args.show_diff and not args.json:
                _print_diffs(last_payload)
            continue

        composed_request = prompt
        if history:
            recent = "\n".join(f"- {entry}" for entry in history[-3:])
            composed_request = (
                f"Contexto recente da sessão interativa:\n{recent}\n\n"
                f"Solicitação atual:\n{prompt}"
            )
        if len(composed_request) < 10:
            composed_request = f"Tarefa solicitada: {composed_request}"
        response = client.post(
            "/workflows",
            content=json.dumps(
                {"project_id": args.project, "request": composed_request},
                ensure_ascii=False,
            ).encode("utf-8"),
        )
        if response.status_code != 202:
            _fail(response)
            continue
        last_workflow_id = response.json()["workflow_id"]
        sys.stderr.write(f"workflow {last_workflow_id}\n")
        _, last_payload = _wait_with_payload(
            client, last_workflow_id, args.timeout, args.poll_seconds, args.json
        )
        if last_payload is not None:
            applied, _ = _extract_applied_diffs(last_payload)
            summary_note = f"Pedido: {prompt}"
            if applied:
                summary_note += f" (arquivos tocados: {', '.join(applied)})"
            history.append(summary_note)
            if args.show_diff and not args.json:
                _print_diffs(last_payload)
    return 0


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
        if args.command == "code":
            return _run_code(client, args)
        if args.command == "chat":
            return _run_chat(client, args)
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
