"""Bounded AST checks for shadowed test definitions; inspected code never runs."""

from __future__ import annotations

import ast
import os
import stat
import time
from pathlib import Path
from typing import Any

from app.agents.validation import ValidationSignal
from app.infrastructure.posix import O_DIRECTORY, O_NOFOLLOW, O_NONBLOCK, PosixRequired, require_posix
from app.models.task import AgentTask, Capability

MAX_FILE_BYTES = 128 * 1024
MAX_TOTAL_BYTES = 2 * 1024 * 1024
MAX_FILES = 100
MAX_FINDINGS = 20
MAX_SECONDS = 5.0


class _Incomplete(Exception):
    pass


def _test_file(path: str) -> bool:
    name = Path(path).name
    return name.endswith(".py") and (name.startswith("test") or name.endswith("_test.py"))


def modified_python_tests(workspace: dict[str, Any]) -> list[str]:
    """Use runtime-produced net diffs, excluding unchanged and deleted paths."""
    diffs = workspace.get("file_diffs")
    if not isinstance(diffs, list):
        return []
    return list(dict.fromkeys(
        item["path"] for item in diffs
        if isinstance(item, dict) and isinstance(item.get("path"), str)
        and item.get("changed") is True and item.get("change_type") != "deleted"
        and _test_file(item["path"])
    ))


def _read_source(root: Path, relative: str) -> bytes:
    require_posix("python_test_integrity")
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts or len(path.parts) > 32:
        raise _Incomplete("unsafe_path")
    fd = os.open(root, os.O_RDONLY | O_DIRECTORY | O_NOFOLLOW)
    try:
        for part in path.parts[:-1]:
            child = os.open(part, os.O_RDONLY | O_DIRECTORY | O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        source = os.open(path.name, os.O_RDONLY | O_NOFOLLOW | O_NONBLOCK, dir_fd=fd)
        with os.fdopen(source, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise _Incomplete("unsafe_source")
            if info.st_size > MAX_FILE_BYTES:
                raise _Incomplete("file_size_limit")
            content = stream.read(MAX_FILE_BYTES + 1)
        if len(content) > MAX_FILE_BYTES:
            raise _Incomplete("file_size_limit")
        return content
    finally:
        os.close(fd)


def _find_shadowing(content: bytes) -> list[str]:
    tree = ast.parse(content, filename="<python-test-integrity>")
    findings: list[str] = []
    nodes = list(ast.walk(tree))
    if len(nodes) > 40_000:
        raise _Incomplete("syntax_size_limit")
    overload_names = {
        alias.asname or alias.name for node in tree.body
        if isinstance(node, ast.ImportFrom) and node.module in {"typing", "typing_extensions"}
        for alias in node.names if alias.name == "overload"
    }
    typing_names = {
        alias.asname or alias.name for node in tree.body if isinstance(node, ast.Import)
        for alias in node.names if alias.name in {"typing", "typing_extensions"}
    }

    def overload(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
        return any(
            isinstance(item, ast.Name) and item.id in overload_names
            or isinstance(item, ast.Attribute) and item.attr == "overload"
            and isinstance(item.value, ast.Name) and item.value.id in typing_names
            for item in node.decorator_list
        )

    def test_definition(node: ast.stmt) -> bool:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return node.name.startswith("test") and not overload(node)
        return isinstance(node, ast.ClassDef) and (
            node.name.startswith("Test") or any(
                isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                and child.name.startswith("test") for child in node.body
            )
        )

    def block(body: list[ast.stmt], scope: str = "") -> None:
        definitions: dict[str, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef] = {}
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and overload(node):
                    continue
                previous = definitions.get(node.name)
                if previous is not None and (test_definition(previous) or test_definition(node)):
                    qualified = f"{scope}.{node.name}" if scope else node.name
                    findings.append(
                        f"{qualified[:128]}: linhas {previous.lineno} e {node.lineno} "
                        "definem o mesmo nome; a última definição oculta testes anteriores"
                    )
                    if len(findings) >= MAX_FINDINGS:
                        return
                definitions[node.name] = node
                if isinstance(node, ast.ClassDef):
                    block(node.body, f"{scope}.{node.name}" if scope else node.name)
            elif isinstance(node, (ast.If, ast.For, ast.AsyncFor, ast.While)):
                # Alternative branches may intentionally define different test
                # implementations. Check each suite independently, not as if all
                # its statements executed unconditionally in sequence.
                block(node.body, scope)
                block(node.orelse, scope)
            elif isinstance(node, (ast.With, ast.AsyncWith)):
                block(node.body, scope)
            elif isinstance(node, (ast.Try, ast.TryStar)):
                for suite in [node.body, node.orelse, node.finalbody, *(h.body for h in node.handlers)]:
                    block(suite, scope)
            elif isinstance(node, ast.Match):
                for case in node.cases:
                    block(case.body, scope)
            if len(findings) >= MAX_FINDINGS:
                return

    block(tree.body)
    return findings


class PythonTestIntegrityValidator:
    name = "python_test_integrity"

    def __init__(self, workspace_root: str):
        # Only composition supplies the root; executor/checkpoint paths cannot.
        self._root = Path(workspace_root).expanduser().resolve()

    async def validate(self, task: AgentTask) -> ValidationSignal:
        result = task.result if isinstance(task.result, dict) else {}
        workspace = result.get("workspace")
        paths = modified_python_tests(workspace) if isinstance(workspace, dict) else []
        signal = await self.run(capability=task.capability, applied_files=paths)
        # Reconciliation may remove a newly created file after a command deleted
        # it. Its failed current-attempt inspection must still veto publication;
        # only another runtime apply/validation replaces that failure evidence.
        feedback = workspace.get("command_feedback", []) if isinstance(workspace, dict) else []
        for item in feedback if isinstance(feedback, list) else []:
            if (isinstance(item, dict) and item.get("name") == self.name
                    and item.get("passed") is False and (signal is None or signal.passed is not False)):
                return ValidationSignal(name=self.name, passed=False,
                                        details=str(item.get("details", "inspeção anterior falhou"))[:8000])
        return signal or ValidationSignal(
            name=self.name, passed=None, details="skipped: nenhum arquivo de teste Python modificado",
        )

    async def run(self, *, capability: Capability, applied_files: list[str]) -> ValidationSignal | None:
        paths = list(dict.fromkeys(path for path in applied_files if _test_file(path)))
        if not paths:
            return None
        deadline = time.monotonic() + MAX_SECONDS
        findings: list[str] = []
        total = 0
        if len(paths) > MAX_FILES:
            return ValidationSignal(name=self.name, passed=False, details="Análise incompleta: file_count_limit")
        for path in paths:
            try:
                if time.monotonic() > deadline:
                    raise _Incomplete("scan_timeout")
                content = _read_source(self._root, path)
                total += len(content)
                if total > MAX_TOTAL_BYTES:
                    raise _Incomplete("total_size_limit")
                findings.extend(f"{path[:256]}: {item}" for item in _find_shadowing(content))
            except (SyntaxError, ValueError, RecursionError, OSError, PosixRequired, _Incomplete) as exc:
                code = str(exc) if isinstance(exc, _Incomplete) else type(exc).__name__
                findings.append(f"{path[:256]}: análise estática incompleta ({code})")
            if len(findings) >= MAX_FINDINGS:
                findings = findings[:MAX_FINDINGS]
                break
        return ValidationSignal(
            name=self.name, passed=not findings,
            details=("Una ou renomeie definições preservando todos os testes; "
                     "não remova cobertura para passar. " + "; ".join(findings))
            if findings else f"AST verificada: {len(paths)} arquivo(s) modificado(s), sem definições de teste duplicadas",
        )
