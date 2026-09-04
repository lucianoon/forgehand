"""Bounded static import checks. Never imports or executes inspected Python files."""

from __future__ import annotations

import ast
import os
import stat
import time
from pathlib import Path
from typing import Any

from app.infrastructure.posix import (
    O_DIRECTORY,
    O_NOFOLLOW,
    O_NONBLOCK,
    require_posix,
)

from app.models.architecture import (
    ArchitectureFinding,
    ArchitecturePolicy,
    ArchitectureReport,
)

MAX_FILES = 1000
MAX_ENTRIES = 10_000
MAX_FILE_BYTES = 128 * 1024
MAX_BYTES = 8 * 1024 * 1024
MAX_DEPTH = 24
MAX_SECONDS = 5.0
IGNORED_DIRECTORIES = frozenset(
    {".git", ".venv", "venv", "node_modules", "__pycache__"}
)


def within(module: str, prefix: str) -> bool:
    return module == prefix or module.startswith(prefix + ".")


class _Incomplete(Exception):
    pass


class _Scan:
    def __init__(self, policy: ArchitecturePolicy):
        self.policy = policy
        self.findings: list[ArchitectureFinding] = []
        self.files = self.entries = self.bytes = 0
        self.complete = True
        self.matched: set[str] = set()
        self.deadline = time.monotonic() + MAX_SECONDS

    def issue(
        self,
        code: str,
        path: str,
        message: str,
        *,
        line: int = 0,
        rule_id: str = "scanner",
        dependency: str = "",
        remediation: str = "Corrija o código ou a configuração aprovada; não ignore esta verificação.",
    ) -> None:
        if len(self.findings) >= 50:
            raise _Incomplete("diagnostic_limit")
        self.findings.append(
            ArchitectureFinding(
                rule_id=rule_id,
                code=code,
                path=path[:256],
                line=line,
                dependency=dependency[:256],
                message=message,
                remediation=remediation,
            )
        )

    def budget(self) -> None:
        if time.monotonic() >= self.deadline:
            raise _Incomplete("scan_timeout")
        if (
            self.entries > MAX_ENTRIES
            or self.files > MAX_FILES
            or self.bytes > MAX_BYTES
        ):
            raise _Incomplete("scan_limit")

    def parse(self, data: bytes, path: str, relative: str) -> None:
        pieces = relative[:-3].split("/")
        is_package = pieces[-1] == "__init__"
        if is_package:
            pieces.pop()
        module = ".".join(pieces)
        rules = [rule for rule in self.policy.rules if within(module, rule.source)]
        self.matched.update(rule.id for rule in rules)
        try:
            tree = ast.parse(data, filename="<architecture-source>")
        except (SyntaxError, ValueError, RecursionError):
            self.complete = False
            self.issue(
                "invalid_python",
                path,
                "Arquivo Python inválido ou não analisável; conteúdo não executado.",
            )
            return
        if not rules:
            return
        package = pieces if is_package else pieces[:-1]
        nodes = list(ast.walk(tree))
        dynamic_names = {"__import__"}
        importlib_names = {"importlib"}
        for node in nodes:
            if isinstance(node, ast.Import):
                importlib_names.update(
                    alias.asname or alias.name
                    for alias in node.names
                    if alias.name == "importlib"
                )
            elif (
                isinstance(node, ast.ImportFrom)
                and node.module == "importlib"
                and node.level == 0
            ):
                dynamic_names.update(
                    alias.asname or alias.name
                    for alias in node.names
                    if alias.name == "import_module"
                )
        for node in nodes:
            self.budget()
            targets: list[str] = []
            if isinstance(node, ast.Import):
                targets = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                if any(alias.name == "*" for alias in node.names) or node.level > len(
                    package
                ):
                    self.issue(
                        "unsupported_import",
                        path,
                        "Import wildcard ou relativo fora do pacote não pode ser certificado.",
                        line=node.lineno,
                        remediation="Use imports estáticos explícitos com o nome completo da dependência.",
                    )
                    continue
                base_parts = (
                    package[: len(package) - node.level + 1] if node.level else []
                )
                base = ".".join([*base_parts, *([node.module] if node.module else [])])
                targets = [
                    base,
                    *(
                        base + "." + alias.name if base else alias.name
                        for alias in node.names
                    ),
                ]
            elif isinstance(node, ast.Call):
                fn = node.func
                if (isinstance(fn, ast.Name) and fn.id in dynamic_names) or (
                    isinstance(fn, ast.Attribute)
                    and fn.attr == "import_module"
                    and isinstance(fn.value, ast.Name)
                    and fn.value.id in importlib_names
                ):
                    self.issue(
                        "unsupported_import",
                        path,
                        "Import dinâmico reconhecido em módulo governado não é suportado.",
                        line=node.lineno,
                        remediation="Substitua por import estático explícito ou solicite revisão da política ao operador.",
                    )
            for rule in rules:
                for target in sorted(set(targets)):
                    if any(within(target, forbidden) for forbidden in rule.forbidden):
                        self.issue(
                            "forbidden_dependency",
                            path,
                            "Dependência atravessa um limite de arquitetura aprovado.",
                            line=getattr(node, "lineno", 0),
                            rule_id=rule.id,
                            dependency=target,
                            remediation=rule.remediation,
                        )
                        break  # One finding per rule/import statement.

    def walk(self, fd: int, path: str, relative: str = "", depth: int = 0) -> None:
        self.budget()
        if depth > MAX_DEPTH:
            raise _Incomplete("depth_limit")
        names: list[str] = []
        with os.scandir(fd) as entries:
            for entry in entries:
                self.entries += 1
                self.budget()
                names.append(entry.name)
        for name in sorted(names):
            item_path = f"{path}/{name}" if path != "." else name
            item_relative = f"{relative}/{name}" if relative else name
            if len(item_path) > 256:
                raise _Incomplete("path_limit")
            mode = os.stat(name, dir_fd=fd, follow_symlinks=False).st_mode
            if stat.S_ISDIR(mode) and name in IGNORED_DIRECTORIES:
                continue
            if stat.S_ISDIR(mode):
                child = os.open(
                    name, os.O_RDONLY | O_DIRECTORY | O_NOFOLLOW, dir_fd=fd
                )
                try:
                    self.walk(child, item_path, item_relative, depth + 1)
                finally:
                    os.close(child)
            elif not stat.S_ISREG(mode):
                self.complete = False
                self.issue(
                    "unsafe_source",
                    item_path,
                    "Link simbólico ou arquivo especial na árvore de fontes; leitura recusada.",
                )
            elif name.endswith(".py"):
                self.files += 1
                self.budget()
                source = os.open(
                    name, os.O_RDONLY | O_NOFOLLOW | O_NONBLOCK, dir_fd=fd
                )
                with os.fdopen(source, "rb") as handle:
                    info = os.fstat(handle.fileno())
                    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                        raise _Incomplete("unsafe_source")
                    if info.st_size > MAX_FILE_BYTES:
                        raise _Incomplete("file_size_limit")
                    data = handle.read(MAX_FILE_BYTES + 1)
                if len(data) > MAX_FILE_BYTES:
                    raise _Incomplete("file_size_limit")
                self.bytes += len(data)
                self.budget()
                self.parse(data, item_path, item_relative)


def check_architecture(root: Path, policy: ArchitecturePolicy) -> ArchitectureReport:
    # dir_fd + O_NOFOLLOW são a garantia contra links trocados: sem POSIX, recusa.
    require_posix("architecture_scan")
    policy = ArchitecturePolicy.model_validate(policy.model_dump())
    scan = _Scan(policy)
    try:
        root_fd = os.open(root, os.O_RDONLY | O_DIRECTORY | O_NOFOLLOW)
        try:
            for source_root in policy.source_roots:
                fd = os.dup(root_fd)
                try:
                    for part in Path(source_root).parts:
                        child = os.open(
                            part,
                            os.O_RDONLY | O_DIRECTORY | O_NOFOLLOW,
                            dir_fd=fd,
                        )
                        os.close(fd)
                        fd = child
                    scan.walk(fd, source_root)
                finally:
                    os.close(fd)
        finally:
            os.close(root_fd)
        for rule in policy.rules:
            if rule.id not in scan.matched:
                scan.issue(
                    "unmatched_rule",
                    ".",
                    "Nenhum módulo corresponde à origem configurada na regra.",
                    rule_id=rule.id,
                    remediation="Confira source_roots e o prefixo source com o operador; a regra não foi exercitada.",
                )
    except (_Incomplete, OSError, RecursionError) as error:
        scan.complete = False
        if len(scan.findings) < 50:
            code = (
                str(error) if isinstance(error, _Incomplete) else "source_unavailable"
            )
            scan.issue(
                code,
                ".",
                "Análise incompleta: fonte insegura, indisponível ou limite atingido.",
                remediation="Corrija a árvore de fontes ou reduza o escopo aprovado e execute novamente.",
            )
    return ArchitectureReport(
        policy_digest=policy.fingerprint(),
        complete=scan.complete,
        files_checked=scan.files,
        findings=tuple(scan.findings),
    )


def architecture_feedback(report: ArchitectureReport) -> list[dict[str, Any]]:
    return [
        {
            "name": f"architecture:{item.rule_id}:{item.code}",
            "passed": False,
            "details": f"{item.path}:{item.line} dependency={item.dependency}",
            "stderr": item.message,
            "stdout": item.remediation,
        }
        for item in report.findings
    ] or [
        {
            "name": "architecture",
            "passed": report.passed,
            "details": f"files={report.files_checked}; complete={report.complete}",
        }
    ]
