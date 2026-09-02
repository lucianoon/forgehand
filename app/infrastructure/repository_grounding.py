"""Coleta determinística de evidências do repositório para grounding.

O objetivo NÃO é resumir o projeto "de cabeça" com LLM, e sim entregar aos
agentes um conjunto pequeno e auditável de trechos reais de arquivos.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any


_TEXT_EXTENSIONS = {
    ".py",
    ".md",
    ".toml",
    ".yaml",
    ".yml",
    ".json",
    ".ini",
    ".cfg",
    ".txt",
    ".env",
    ".sql",
}
_ALWAYS_INCLUDE = {
    "README.md",
    "README.rst",
    "pyproject.toml",
    "docker-compose.yml",
    "docker-compose.yaml",
    ".env.example",
}
_IGNORED_DIRS = {
    ".git",
    ".venv",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "__pycache__",
    "node_modules",
    "dist",
    "build",
    ".idea",
    ".vscode",
}


# Aliases públicos para as ferramentas dos agentes compartilharem a mesma
# noção de "arquivo de texto" e "diretório ignorado".
TEXT_EXTENSIONS = _TEXT_EXTENSIONS
IGNORED_DIRS = _IGNORED_DIRS


def _is_probably_text(path: Path) -> bool:
    return path.suffix.lower() in _TEXT_EXTENSIONS or path.name in _ALWAYS_INCLUDE


def _keywordize(text: str) -> list[str]:
    words = re.findall(r"[a-z0-9_./-]+", text.lower())
    return [word for word in words if len(word) >= 3]


def _safe_read(path: Path, max_file_bytes: int) -> str:
    if path.stat().st_size > max_file_bytes:
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        try:
            return path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return ""
    except OSError:
        return ""


def _extract_excerpt(
    text: str, keywords: list[str], max_excerpt_lines: int
) -> tuple[str, int, int]:
    lines = text.splitlines()
    if not lines:
        return "", 1, 1

    if not keywords:
        end = min(len(lines), max_excerpt_lines)
        excerpt = "\n".join(lines[:end])
        return excerpt, 1, end

    lower_lines = [line.lower() for line in lines]
    match_indexes = [
        index
        for index, line in enumerate(lower_lines)
        if any(keyword in line for keyword in keywords)
    ]
    if not match_indexes:
        end = min(len(lines), max_excerpt_lines)
        excerpt = "\n".join(lines[:end])
        return excerpt, 1, end

    first = max(0, match_indexes[0] - 3)
    last = min(len(lines), first + max_excerpt_lines)
    excerpt = "\n".join(lines[first:last])
    return excerpt, first + 1, last


@dataclass(frozen=True)
class _Candidate:
    path: Path
    relative_path: str
    score: int
    text: str


class RepositoryGroundingCollector:
    def __init__(
        self,
        repo_root: str,
        *,
        max_files: int = 16,
        max_excerpt_lines: int = 60,
        max_file_bytes: int = 64_000,
        full_file_max_bytes: int = 0,
    ) -> None:
        self._repo_root = Path(repo_root).expanduser().resolve()
        self._max_files = max_files
        self._max_excerpt_lines = max_excerpt_lines
        self._max_file_bytes = max_file_bytes
        self._full_file_max_bytes = full_file_max_bytes

    def collect(self, request: str) -> dict[str, Any]:
        keywords = _keywordize(request)
        candidates = self._rank_candidates(keywords)
        top_level = [
            entry.name
            for entry in sorted(
                self._repo_root.iterdir(), key=lambda item: item.name.lower()
            )
            if entry.name not in _IGNORED_DIRS
        ][:20]

        evidence: list[dict[str, Any]] = []
        for index, candidate in enumerate(candidates[: self._max_files], start=1):
            if (
                self._full_file_max_bytes > 0
                and len(candidate.text.encode("utf-8")) <= self._full_file_max_bytes
            ):
                # Arquivo inteiro: permite op=replace em qualquer ponto dele.
                excerpt = candidate.text
                line_start, line_end = 1, max(1, len(candidate.text.splitlines()))
            else:
                excerpt, line_start, line_end = _extract_excerpt(
                    candidate.text,
                    keywords,
                    self._max_excerpt_lines,
                )
            if not excerpt.strip():
                continue
            evidence.append(
                {
                    "id": f"E{index}",
                    "path": candidate.relative_path,
                    "line_start": line_start,
                    "line_end": line_end,
                    "score": candidate.score,
                    "excerpt": excerpt,
                }
            )

        return {
            "repo_root": str(self._repo_root),
            "query": request,
            "keywords": keywords[:12],
            "top_level_entries": top_level,
            "require_citations": True,
            "evidence": evidence,
        }

    def _rank_candidates(self, keywords: list[str]) -> list[_Candidate]:
        candidates: list[_Candidate] = []
        for path in self._repo_root.rglob("*"):
            if not path.is_file():
                continue
            if any(part in _IGNORED_DIRS for part in path.parts):
                continue
            if not _is_probably_text(path):
                continue

            relative_path = path.relative_to(self._repo_root).as_posix()
            text = _safe_read(path, self._max_file_bytes)
            if not text.strip():
                continue

            score = self._score_candidate(relative_path, text, keywords)
            candidates.append(
                _Candidate(
                    path=path,
                    relative_path=relative_path,
                    score=score,
                    text=text,
                )
            )

        candidates.sort(
            key=lambda item: (
                -item.score,
                len(item.relative_path.split("/")),
                item.relative_path,
            )
        )
        return candidates

    @staticmethod
    def _score_candidate(relative_path: str, text: str, keywords: list[str]) -> int:
        lower_path = relative_path.lower()
        lower_text = text.lower()
        score = 0

        if relative_path in _ALWAYS_INCLUDE:
            score += 200
        if lower_path.startswith("app/"):
            score += 120
        if lower_path.startswith("tests/"):
            score += 60
        if lower_path.endswith("readme.md"):
            score += 180
        if "workflow" in lower_path or "agent" in lower_path:
            score += 40
        if "graph" in lower_path or "provider" in lower_path:
            score += 20

        for keyword in keywords:
            score += lower_path.count(keyword) * 30
            score += lower_text.count(keyword) * 2

        return score
