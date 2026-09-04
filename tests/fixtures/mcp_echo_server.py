"""Servidor MCP mínimo (stdio, JSON-RPC por linha) para os testes.

Ferramentas: echo (devolve o texto), secret_env (devolve ANTHROPIC_API_KEY do
ambiente ou "ausente"), fail (isError) e hidden (não deve ser exposta quando
allowed_tools filtra).
"""

import json
import os
import sys

TOOLS = [
    {
        "name": "echo",
        "description": "Devolve o texto recebido",
        "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
    },
    {"name": "secret_env", "description": "Lê ANTHROPIC_API_KEY", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "fail", "description": "Sempre falha", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "hidden", "description": "Não deveria aparecer", "inputSchema": {"type": "object", "properties": {}}},
]


def reply(message_id, result=None, error=None):
    payload = {"jsonrpc": "2.0", "id": message_id}
    if error is not None:
        payload["error"] = error
    else:
        payload["result"] = result
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        message = json.loads(line)
        method = message.get("method")
        message_id = message.get("id")
        if method == "initialize":
            reply(message_id, {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}, "serverInfo": {"name": "echo", "version": "1"}})
        elif method == "notifications/initialized":
            continue
        elif method == "tools/list":
            reply(message_id, {"tools": TOOLS})
        elif method == "tools/call":
            params = message.get("params") or {}
            name = params.get("name")
            arguments = params.get("arguments") or {}
            if name == "echo":
                reply(message_id, {"content": [{"type": "text", "text": f"eco: {arguments.get('text', '')}"}]})
            elif name == "secret_env":
                reply(message_id, {"content": [{"type": "text", "text": os.environ.get("ANTHROPIC_API_KEY", "ausente")}]})
            elif name == "fail":
                reply(message_id, {"content": [{"type": "text", "text": "quebrou de propósito"}], "isError": True})
            else:
                reply(message_id, error={"code": -32602, "message": f"ferramenta desconhecida: {name}"})
        else:
            reply(message_id, error={"code": -32601, "message": f"método desconhecido: {method}"})


if __name__ == "__main__":
    main()
