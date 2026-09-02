from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Any

import httpx


class SCMError(RuntimeError):
    pass


@dataclass(frozen=True)
class PullRequestResult:
    number: int
    url: str
    branch: str


def collect_publishable_changes(
    tasks: list[dict[str, Any]],
) -> tuple[list[dict[str, str]], list[str]]:
    """Extrai (arquivos finais, remoções) dos resultados das tarefas.

    Fonte preferida: `workspace.published_files` / `workspace.deleted_paths`
    (conteúdo FINAL após create/replace/delete). Fallback: `files` legado do
    payload do executor (arquivo inteiro). Última escrita de um path vence;
    remoção posterior anula publicação anterior e vice-versa."""
    latest: dict[str, str | None] = {}
    for task in tasks:
        result = task.get("result")
        if not isinstance(result, dict):
            continue
        workspace = result.get("workspace")
        source: list[Any] = []
        deleted: list[Any] = []
        if isinstance(workspace, dict) and isinstance(
            workspace.get("published_files"), list
        ):
            source = workspace["published_files"]
            if isinstance(workspace.get("deleted_paths"), list):
                deleted = workspace["deleted_paths"]
        elif isinstance(result.get("files"), list):
            source = result["files"]
        for item in source:
            if (
                isinstance(item, dict)
                and isinstance(item.get("path"), str)
                and isinstance(item.get("content"), str)
            ):
                latest[item["path"]] = item["content"]
        for path in deleted:
            if isinstance(path, str):
                latest[path] = None
    files = [
        {"path": path, "content": content}
        for path, content in latest.items()
        if content is not None
    ]
    deletions = [path for path, content in latest.items() if content is None]
    return files, deletions


class GitHubSCMClient:
    """Publica artefatos do workflow em uma branch e abre um pull request."""

    def __init__(
        self,
        token: str,
        *,
        client: httpx.AsyncClient | None = None,
        base_url: str = "https://api.github.com",
    ) -> None:
        if not token:
            raise ValueError("GITHUB_TOKEN não configurado.")
        self._client = client or httpx.AsyncClient(
            base_url=base_url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )

    async def publish_pull_request(
        self,
        *,
        repository: str,
        base_branch: str,
        head_branch: str,
        title: str,
        body: str,
        files: list[dict[str, str]],
        deletions: list[str] | None = None,
    ) -> PullRequestResult:
        if "/" not in repository:
            raise ValueError("Repositório deve usar o formato owner/name.")
        deletions = deletions or []
        if not files and not deletions:
            raise ValueError("Workflow sem arquivos publicáveis.")

        base_ref = await self._request(
            "GET", f"/repos/{repository}/git/ref/heads/{base_branch}"
        )
        base_sha = base_ref["object"]["sha"]
        if not await self._branch_exists(repository, head_branch):
            await self._request(
                "POST",
                f"/repos/{repository}/git/refs",
                json={"ref": f"refs/heads/{head_branch}", "sha": base_sha},
            )

        unique_files = {
            artifact.get("path", "").strip(): artifact.get("content")
            for artifact in files
        }
        for path, content in unique_files.items():
            if not path or not isinstance(content, str):
                raise ValueError("Artefato SCM exige path e content.")
            existing = await self._existing_file(repository, path, head_branch)
            if existing is not None and existing[1] == content:
                continue
            payload: dict[str, Any] = {
                "message": f"forgehand: update {path}",
                "content": base64.b64encode(content.encode()).decode(),
                "branch": head_branch,
            }
            if existing is not None:
                payload["sha"] = existing[0]
            await self._request(
                "PUT", f"/repos/{repository}/contents/{path}", json=payload
            )

        for path in deletions:
            existing = await self._existing_file(repository, path, head_branch)
            if existing is None:
                continue  # já não existe na branch: idempotente
            await self._request(
                "DELETE",
                f"/repos/{repository}/contents/{path}",
                json={
                    "message": f"forgehand: delete {path}",
                    "sha": existing[0],
                    "branch": head_branch,
                },
            )

        existing_pull = await self._existing_pull_request(
            repository, base_branch, head_branch
        )
        if existing_pull is not None:
            return PullRequestResult(
                number=int(existing_pull["number"]),
                url=str(existing_pull["html_url"]),
                branch=head_branch,
            )

        pull = await self._request(
            "POST",
            f"/repos/{repository}/pulls",
            json={
                "title": title,
                "body": body,
                "head": head_branch,
                "base": base_branch,
            },
        )
        return PullRequestResult(
            number=int(pull["number"]),
            url=str(pull["html_url"]),
            branch=head_branch,
        )

    async def _branch_exists(self, repository: str, branch: str) -> bool:
        response = await self._client.get(f"/repos/{repository}/git/ref/heads/{branch}")
        if response.status_code == 404:
            return False
        self._raise_for_status(response)
        return True

    async def _existing_file(
        self, repository: str, path: str, branch: str
    ) -> tuple[str, str] | None:
        response = await self._client.get(
            f"/repos/{repository}/contents/{path}", params={"ref": branch}
        )
        if response.status_code == 404:
            return None
        self._raise_for_status(response)
        payload = response.json()
        encoded = str(payload.get("content", "")).replace("\n", "")
        content = base64.b64decode(encoded).decode() if encoded else ""
        return str(payload["sha"]), content

    async def _existing_pull_request(
        self, repository: str, base_branch: str, head_branch: str
    ) -> dict[str, Any] | None:
        owner = repository.split("/", 1)[0]
        response = await self._client.get(
            f"/repos/{repository}/pulls",
            params={
                "state": "open",
                "head": f"{owner}:{head_branch}",
                "base": base_branch,
            },
        )
        self._raise_for_status(response)
        payload = response.json()
        if not isinstance(payload, list):
            raise SCMError("Resposta SCM inesperada ao consultar pull requests.")
        return payload[0] if payload else None

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        response = await self._client.request(method, path, **kwargs)
        self._raise_for_status(response)
        payload = response.json()
        if not isinstance(payload, dict):
            raise SCMError("Resposta SCM inesperada.")
        return payload

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        if response.status_code >= 400:
            raise SCMError(
                f"GitHub respondeu {response.status_code}: {response.text[:300]}"
            )

    async def close(self) -> None:
        await self._client.aclose()
