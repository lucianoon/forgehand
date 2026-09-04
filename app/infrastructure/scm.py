"""Publicação de entregas no GitHub — o último trecho do ciclo.

O que muda em relação à versão por Contents API:

- COMMIT ATÔMICO: todos os arquivos e remoções entram em uma única árvore
  (Git Data API: trees → commit → ref). Um commit por publicação, não um por
  arquivo; retry com o mesmo conteúdo não cria commit (árvore idêntica).
- CI COMO SINAL OBJETIVO: `wait_for_checks` acompanha check runs e statuses
  do commit publicado e devolve estado + falhas legíveis (título, resumo e
  anotações) para realimentar o replan.
- CREDENCIAL POR PROVIDER: token estático (GITHUB_TOKEN) ou GitHub App
  (token de instalação curto, renovado sob demanda). O grafo nunca vê o
  token — recebe um DeliveryPublisher.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal, Protocol
from urllib.parse import quote

import httpx

from app.graph.state import DeliveryConfig, DeliveryResult
from app.models.factory import GitHubIssueSnapshot
from app.models.product_delivery import MergeReceipt, ProductDeliveryPlan

CheckState = Literal["success", "failure", "pending", "none"]
_SUCCESS_CONCLUSIONS = {"success", "neutral", "skipped"}
_FORGEHAND_STATUS_PREFIX = "forgehand/"


class SCMError(RuntimeError):
    pass


@dataclass(frozen=True)
class PullRequestResult:
    number: int
    url: str
    branch: str
    commit_sha: str
    changed: bool  # False quando a árvore já era idêntica (retry sem mudança)


@dataclass(frozen=True)
class CheckRun:
    name: str
    status: str  # queued | in_progress | completed
    conclusion: str | None
    details_url: str | None = None
    summary: str = ""

    @property
    def completed(self) -> bool:
        return self.status == "completed"

    @property
    def passed(self) -> bool | None:
        if not self.completed:
            return None
        return self.conclusion in _SUCCESS_CONCLUSIONS


@dataclass(frozen=True)
class CheckRunsResult:
    state: CheckState
    checks: list[CheckRun] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)  # prontas para o agente
    failure_paths: list[str] = field(default_factory=list)

    def as_dicts(self) -> list[dict[str, Any]]:
        return [
            {
                "name": c.name,
                "status": c.status,
                "conclusion": c.conclusion,
                "details_url": c.details_url,
                "summary": c.summary,
            }
            for c in self.checks
        ]


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


def task_publishes_changes(task_result: Any) -> bool:
    """A tarefa contribuiu com arquivos/remoções para a entrega? Usado para
    saber quais tarefas reabrir quando o CI falha."""
    files, deletions = collect_publishable_changes([{"result": task_result}])
    return bool(files or deletions)


# --------------------------------------------------------------------------
# Credenciais
# --------------------------------------------------------------------------


class TokenProvider(Protocol):
    async def token(self) -> str: ...


class StaticTokenProvider:
    def __init__(self, token: str) -> None:
        if not token:
            raise ValueError("GITHUB_TOKEN não configurado.")
        self._token = token

    async def token(self) -> str:
        return self._token


class GitHubAppTokenProvider:
    """Token de instalação de GitHub App: JWT RS256 assinado com a chave
    privada do app → POST /app/installations/{id}/access_tokens. O token
    vale ~1h e é renovado 60s antes de expirar. Escopo = permissões da
    instalação (por repositório), sem PAT de usuário compartilhado."""

    def __init__(
        self,
        app_id: str,
        installation_id: str,
        private_key_pem: str,
        *,
        client: httpx.AsyncClient | None = None,
        base_url: str = "https://api.github.com",
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not (app_id and installation_id and private_key_pem):
            raise ValueError(
                "GitHub App exige app_id, installation_id e chave privada."
            )
        self._app_id = app_id
        self._installation_id = installation_id
        self._private_key = private_key_pem
        self._client = client or httpx.AsyncClient(base_url=base_url)
        self._clock = clock
        self._cached: tuple[str, float] | None = None  # (token, expires_at)
        self._lock = asyncio.Lock()

    def _app_jwt(self) -> str:
        try:
            import jwt  # PyJWT — extra [github-app]
        except ImportError as exc:  # pragma: no cover - depende do ambiente
            raise SCMError(
                "GitHub App exige PyJWT: instale o extra `github-app`."
            ) from exc
        now = int(self._clock())
        payload = {"iat": now - 60, "exp": now + 540, "iss": self._app_id}
        return str(jwt.encode(payload, self._private_key, algorithm="RS256"))

    async def token(self) -> str:
        async with self._lock:
            if self._cached and self._cached[1] - 60 > self._clock():
                return self._cached[0]
            response = await self._client.post(
                f"/app/installations/{self._installation_id}/access_tokens",
                headers={
                    "Authorization": f"Bearer {self._app_jwt()}",
                    "Accept": "application/vnd.github+json",
                },
            )
            if response.status_code >= 400:
                raise SCMError(
                    f"GitHub App: {response.status_code}: {response.text[:300]}"
                )
            data = response.json()
            token = str(data["token"])
            expires_at = _parse_iso(data.get("expires_at")) or (self._clock() + 3000)
            self._cached = (token, expires_at)
            return token

    async def close(self) -> None:
        await self._client.aclose()


def _parse_iso(value: Any) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def build_token_provider_from_env(
    env: Any = None, *, client: httpx.AsyncClient | None = None
) -> TokenProvider | None:
    """GitHub App tem prioridade sobre GITHUB_TOKEN. A chave pode vir inline
    (GITHUB_APP_PRIVATE_KEY, com \\n literais) ou por arquivo
    (GITHUB_APP_PRIVATE_KEY_PATH). Segredos lidos aqui, nunca via Settings."""
    env = os.environ if env is None else env
    app_id = env.get("GITHUB_APP_ID", "")
    installation_id = env.get("GITHUB_APP_INSTALLATION_ID", "")
    key = env.get("GITHUB_APP_PRIVATE_KEY", "").replace("\\n", "\n")
    key_path = env.get("GITHUB_APP_PRIVATE_KEY_PATH", "")
    if not key and key_path:
        try:
            with open(key_path, encoding="utf-8") as handle:
                key = handle.read()
        except OSError as exc:
            raise SCMError(f"Não foi possível ler {key_path}: {exc}") from exc
    if app_id and installation_id and key:
        return GitHubAppTokenProvider(app_id, installation_id, key, client=client)
    token = env.get("GITHUB_TOKEN", "")
    if token:
        return StaticTokenProvider(token)
    return None


# --------------------------------------------------------------------------
# Cliente
# --------------------------------------------------------------------------


class GitHubSCMClient:
    """Publica uma entrega como UM commit em uma branch e abre/reutiliza o PR;
    acompanha os checks do commit."""

    def __init__(
        self,
        token: str | None = None,
        *,
        token_provider: TokenProvider | None = None,
        client: httpx.AsyncClient | None = None,
        base_url: str = "https://api.github.com",
    ) -> None:
        if token_provider is None:
            token_provider = StaticTokenProvider(token or "")
        self._tokens = token_provider
        self._client = client or httpx.AsyncClient(
            base_url=base_url,
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )

    async def fetch_issue_snapshot(
        self,
        *,
        repository: str,
        issue_number: int,
        source_url: str,
    ) -> GitHubIssueSnapshot:
        """Lê uma issue usando apenas a credencial da instalação ativa."""
        if not re.fullmatch(r"[^/\s]+/[^/\s]+", repository):
            raise ValueError("Repositório deve usar o formato owner/name.")
        if issue_number <= 0:
            raise ValueError("Número da issue deve ser positivo.")
        payload = await self._request(
            "GET", f"/repos/{repository}/issues/{issue_number}"
        )
        if "pull_request" in payload:
            raise SCMError("A URL informada aponta para um pull request, não issue.")
        user = payload.get("user")
        author = user.get("login") if isinstance(user, dict) else None
        labels = payload.get("labels", [])
        if not isinstance(labels, list):
            labels = []
        label_names = [
            str(item["name"])
            for item in labels
            if isinstance(item, dict) and item.get("name")
        ]
        try:
            return GitHubIssueSnapshot.model_validate(
                {
                    "url": str(payload.get("html_url") or source_url),
                    "number": int(payload["number"]),
                    "title": str(payload["title"]),
                    "body": str(payload.get("body") or ""),
                    "labels": label_names,
                    "repository": repository,
                    "author": str(author or "unknown"),
                    "updated_at": datetime.fromisoformat(
                        str(payload["updated_at"]).replace("Z", "+00:00")
                    ),
                }
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise SCMError("Resposta da issue está incompleta ou inválida.") from exc

    async def delivery_base(
        self, repository: str, branch: str, ancestor: str | None = None
    ) -> str:
        """Read and pin the base, requiring a prior merge to remain in its history."""
        if not re.fullmatch(r"[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+", repository):
            raise SCMError("Invalid delivery repository")
        ProductDeliveryPlan.repository_name(repository)
        ProductDeliveryPlan.branch_name(branch)
        payload = await self._request(
            "GET", f"/repos/{repository}/git/ref/heads/{quote(branch, safe='')}"
        )
        sha = payload.get("object", {}).get("sha")
        if not isinstance(sha, str) or not re.fullmatch(r"[a-f0-9]{40}", sha):
            raise SCMError("Invalid delivery base SHA")
        if ancestor is not None:
            if not re.fullmatch(r"[a-f0-9]{40}", ancestor):
                raise SCMError("Invalid delivery ancestor")
            comparison = await self._request(
                "GET", f"/repos/{repository}/compare/{ancestor}...{sha}"
            )
            if comparison.get("status") not in {"ahead", "identical"}:
                raise SCMError("Previous delivery is absent from the base history")
        return sha

    async def verified_delivery_merge(
        self, repository: str, branch: str, number: int, head_sha: str
    ) -> MergeReceipt | None:
        """Observe only: never merges. Green CI alone is not incorporation evidence."""
        ProductDeliveryPlan.repository_name(repository)
        if not re.fullmatch(r"[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+", repository):
            raise SCMError("Invalid delivery repository")
        ProductDeliveryPlan.branch_name(branch)
        if number <= 0 or not re.fullmatch(r"[a-f0-9]{40}", head_sha):
            raise SCMError("Invalid delivery receipt")
        pr = await self._request("GET", f"/repos/{repository}/pulls/{number}")
        base, head = pr.get("base") or {}, pr.get("head") or {}
        if (base.get("ref") != branch
                or (base.get("repo") or {}).get("full_name", "").lower() != repository.lower()
                or head.get("sha") != head_sha):
            raise SCMError("Pull request differs from recorded delivery")
        if pr.get("merged") is not True:
            return None
        merge_sha = pr.get("merge_commit_sha")
        if not isinstance(merge_sha, str) or not re.fullmatch(r"[a-f0-9]{40}", merge_sha):
            raise SCMError("Missing merge commit")
        base_sha = await self.delivery_base(repository, branch, merge_sha)
        return MergeReceipt(pull_request_number=number, commit_sha=head_sha,
                            merge_commit_sha=merge_sha, base_sha=base_sha)

    # ------------------------------------------------------------ publicação
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
        commit_message: str | None = None,
        pinned_base_sha: str | None = None,
        expected_head_sha: str | None = None,
    ) -> PullRequestResult:
        if "/" not in repository:
            raise ValueError("Repositório deve usar o formato owner/name.")
        deletions = list(deletions or [])
        if not files and not deletions:
            raise ValueError("Workflow sem arquivos publicáveis.")
        for artifact in files:
            if not artifact.get("path", "").strip() or not isinstance(
                artifact.get("content"), str
            ):
                raise ValueError("Artefato SCM exige path e content.")

        if pinned_base_sha is not None:
            if not re.fullmatch(r"[0-9a-f]{40}", pinned_base_sha):
                raise ValueError("factory_base_sha_invalid")
            if head_branch == base_branch:
                raise ValueError("factory_head_must_differ_from_base")
            base_sha = pinned_base_sha
        else:
            base_ref = await self._request(
                "GET", f"/repos/{repository}/git/ref/heads/{base_branch}"
            )
            base_sha = str(base_ref["object"]["sha"])
        head_sha = await self._branch_sha(repository, head_branch)
        publication_message = commit_message or f"forgehand: {title}"
        recovery_commit: dict[str, Any] | None = None
        if pinned_base_sha is not None:
            intent = json.dumps(
                [
                    repository,
                    base_branch,
                    head_branch,
                    pinned_base_sha,
                    expected_head_sha,
                    sorted(files, key=lambda item: item["path"]),
                    sorted(deletions),
                ],
                sort_keys=True,
                separators=(",", ":"),
            )
            marker = hashlib.sha256(intent.encode()).hexdigest()
            publication_message += f"\n\nForgehand-Intent: {marker}"
            if head_sha != expected_head_sha:
                if head_sha is None:
                    raise SCMError("factory_head_mismatch")
                recovery_commit = await self._request(
                    "GET", f"/repos/{repository}/git/commits/{head_sha}"
                )
                if recovery_commit.get("message") != publication_message or [
                    parent.get("sha") for parent in recovery_commit.get("parents", [])
                ] != [expected_head_sha or pinned_base_sha]:
                    raise SCMError("factory_head_mismatch")
        branch_exists = head_sha is not None
        # Branch nova só é criada DEPOIS do commit, já apontando para ele: criar
        # a ref na base e depois movê-la disparava um CI vermelho inútil no
        # commit antigo (visto na primeira rodada real).
        parent_sha = head_sha or base_sha
        parent_commit = recovery_commit or await self._request(
            "GET", f"/repos/{repository}/git/commits/{parent_sha}"
        )
        parent_tree = str(parent_commit["tree"]["sha"])
        base_tree = parent_tree
        if pinned_base_sha is not None and parent_sha != pinned_base_sha:
            pinned_commit = await self._request(
                "GET", f"/repos/{repository}/git/commits/{pinned_base_sha}"
            )
            base_tree = str(pinned_commit["tree"]["sha"])

        entries: list[dict[str, Any]] = [
            {
                "path": artifact["path"].strip(),
                "mode": "100644",
                "type": "blob",
                "content": artifact["content"],
            }
            for artifact in files
        ]
        for path in deletions:
            # remover path ausente é erro 422 na API; pular mantém idempotência
            if await self._path_exists(
                repository, path, base_sha if pinned_base_sha else parent_sha
            ):
                entries.append(
                    {"path": path, "mode": "100644", "type": "blob", "sha": None}
                )

        commit_sha = parent_sha
        changed = False
        if entries or pinned_base_sha is not None:
            tree = await self._request(
                "POST",
                f"/repos/{repository}/git/trees",
                json={"base_tree": base_tree, "tree": entries},
            )
            new_tree = str(tree["sha"])
            if recovery_commit is not None and new_tree != parent_tree:
                raise SCMError("factory_recovery_tree_mismatch")
            if new_tree != parent_tree:
                commit = await self._request(
                    "POST",
                    f"/repos/{repository}/git/commits",
                    json={
                        "message": publication_message,
                        "tree": new_tree,
                        "parents": [parent_sha],
                    },
                )
                commit_sha = str(commit["sha"])
                changed = True
        if not branch_exists:
            await self._request(
                "POST",
                f"/repos/{repository}/git/refs",
                json={"ref": f"refs/heads/{head_branch}", "sha": commit_sha},
            )
        elif changed:
            await self._request(
                "PATCH",
                f"/repos/{repository}/git/refs/heads/{head_branch}",
                json={"sha": commit_sha, "force": False},
            )

        existing_pull = await self._existing_pull_request(
            repository,
            base_branch,
            head_branch,
            include_closed=pinned_base_sha is not None,
        )
        if existing_pull is not None:
            return PullRequestResult(
                number=int(existing_pull["number"]),
                url=str(existing_pull["html_url"]),
                branch=head_branch,
                commit_sha=commit_sha,
                changed=changed,
            )
        try:
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
        except (SCMError, httpx.HTTPError):
            # A criação pode ter sido aceita antes de perdermos a resposta.
            # Consulta a identidade exata; nunca abre outro PR como fallback.
            recovered = await self._existing_pull_request(
                repository,
                base_branch,
                head_branch,
                include_closed=pinned_base_sha is not None,
            )
            if recovered is None:
                raise
            pull = recovered
        return PullRequestResult(
            number=int(pull["number"]),
            url=str(pull["html_url"]),
            branch=head_branch,
            commit_sha=commit_sha,
            changed=changed,
        )

    # ---------------------------------------------------------------- checks
    async def fetch_checks(self, repository: str, sha: str) -> CheckRunsResult:
        """Snapshot dos check runs (Checks API) + statuses (Status API) do
        commit. Statuses publicados pelo próprio Forgehand são ignorados."""
        runs_payload = await self._request(
            "GET",
            f"/repos/{repository}/commits/{sha}/check-runs",
            params={"per_page": 100},
        )
        checks: list[CheckRun] = []
        for run in runs_payload.get("check_runs", []) or []:
            output = run.get("output") or {}
            summary_parts = [
                str(part)
                for part in (output.get("title"), output.get("summary"))
                if part
            ]
            checks.append(
                CheckRun(
                    name=str(run.get("name", "check")),
                    status=str(run.get("status", "queued")),
                    conclusion=run.get("conclusion"),
                    details_url=run.get("details_url") or run.get("html_url"),
                    summary=" — ".join(summary_parts)[:500],
                )
            )
        status_payload = await self._request(
            "GET", f"/repos/{repository}/commits/{sha}/status"
        )
        for status in status_payload.get("statuses", []) or []:
            context = str(status.get("context", "status"))
            if context.startswith(_FORGEHAND_STATUS_PREFIX):
                continue
            state = str(status.get("state", "pending"))
            checks.append(
                CheckRun(
                    name=context,
                    status="completed" if state != "pending" else "in_progress",
                    conclusion=None
                    if state == "pending"
                    else ("success" if state == "success" else "failure"),
                    details_url=status.get("target_url"),
                    summary=str(status.get("description") or "")[:500],
                )
            )

        if not checks:
            return CheckRunsResult(state="none")
        if any(not c.completed for c in checks):
            return CheckRunsResult(state="pending", checks=checks)
        failed = [c for c in checks if c.passed is False]
        if not failed:
            return CheckRunsResult(state="success", checks=checks)
        failures: list[str] = []
        for check in failed:
            line = f"{check.name}: {check.conclusion}"
            if check.summary:
                line += f" — {check.summary}"
            failures.append(line)
        annotations, paths = await self._annotations(repository, runs_payload, failed)
        failures.extend(annotations)
        return CheckRunsResult(
            state="failure", checks=checks, failures=failures, failure_paths=paths
        )

    async def _annotations(
        self, repository: str, runs_payload: dict[str, Any], failed: list[CheckRun]
    ) -> tuple[list[str], list[str]]:
        """Anotações (path:linha mensagem) dos check runs que falharam —
        o feedback mais acionável que o CI oferece."""
        failed_names = {c.name for c in failed}
        lines: list[str] = []
        paths: set[str] = set()
        for run in runs_payload.get("check_runs", []) or []:
            if run.get("name") not in failed_names or not run.get("id"):
                continue
            try:
                annotations = await self._request_list(
                    "GET",
                    f"/repos/{repository}/check-runs/{run['id']}/annotations",
                    params={"per_page": 20},
                )
            except SCMError:
                continue
            for annotation in annotations[:20]:
                path = annotation.get("path", "")
                if isinstance(path, str) and path and not path.startswith("/"):
                    if ".." not in path.split("/"):
                        paths.add(path)
                start = annotation.get("start_line", "")
                message = str(annotation.get("message", "")).strip()
                if message:
                    lines.append(f"{run.get('name')}: {path}:{start} {message[:300]}")
        return lines, sorted(paths)

    async def wait_for_checks(
        self,
        repository: str,
        sha: str,
        *,
        timeout_seconds: float = 900.0,
        poll_interval_seconds: float = 15.0,
        grace_seconds: float = 90.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> CheckRunsResult:
        """Espera todos os checks concluírem. Sem nenhum check após o período
        de graça → `none` (repositório sem CI). Timeout → `pending` com o que
        se sabe; quem chama decide o que fazer com isso."""
        started = clock()
        last = CheckRunsResult(state="none")
        while True:
            last = await self.fetch_checks(repository, sha)
            elapsed = clock() - started
            if last.state in {"success", "failure"}:
                return last
            if last.state == "none" and elapsed >= grace_seconds:
                return last
            if elapsed >= timeout_seconds:
                return (
                    last
                    if last.state == "pending"
                    else CheckRunsResult(state="pending", checks=last.checks)
                )
            await sleep(poll_interval_seconds)

    async def post_commit_status(
        self,
        repository: str,
        sha: str,
        *,
        state: Literal["pending", "success", "failure", "error"],
        description: str,
        context: str = f"{_FORGEHAND_STATUS_PREFIX}delivery",
        target_url: str | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "state": state,
            "description": description[:140],
            "context": context,
        }
        if target_url:
            payload["target_url"] = target_url
        await self._request("POST", f"/repos/{repository}/statuses/{sha}", json=payload)

    # -------------------------------------------------------------- helpers
    async def _branch_sha(self, repository: str, branch: str) -> str | None:
        response = await self._raw("GET", f"/repos/{repository}/git/ref/heads/{branch}")
        if response.status_code == 404:
            return None
        self._raise_for_status(response)
        payload = response.json()
        return str(payload["object"]["sha"])

    async def _path_exists(self, repository: str, path: str, ref: str) -> bool:
        response = await self._raw(
            "GET", f"/repos/{repository}/contents/{path}", params={"ref": ref}
        )
        if response.status_code == 404:
            return False
        self._raise_for_status(response)
        return True

    async def _existing_pull_request(
        self,
        repository: str,
        base_branch: str,
        head_branch: str,
        *,
        include_closed: bool = False,
    ) -> dict[str, Any] | None:
        owner = repository.split("/", 1)[0]
        pulls = await self._request_list(
            "GET",
            f"/repos/{repository}/pulls",
            params={
                "state": "all" if include_closed else "open",
                "head": f"{owner}:{head_branch}",
                "base": base_branch,
            },
        )
        if include_closed and any(pull.get("state") == "closed" for pull in pulls):
            raise SCMError("factory_pull_already_closed")
        return pulls[0] if pulls else None

    async def _raw(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        headers = dict(kwargs.pop("headers", {}) or {})
        headers["Authorization"] = f"Bearer {await self._tokens.token()}"
        return await self._client.request(method, path, headers=headers, **kwargs)

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        response = await self._raw(method, path, **kwargs)
        self._raise_for_status(response)
        payload = response.json() if response.content else {}
        if not isinstance(payload, dict):
            raise SCMError("Resposta SCM inesperada.")
        return payload

    async def _request_list(
        self, method: str, path: str, **kwargs: Any
    ) -> list[dict[str, Any]]:
        response = await self._raw(method, path, **kwargs)
        self._raise_for_status(response)
        payload = response.json()
        if not isinstance(payload, list):
            raise SCMError("Resposta SCM inesperada (lista esperada).")
        return payload

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        if response.status_code >= 400:
            raise SCMError(
                f"GitHub respondeu {response.status_code}: {response.text[:300]}"
            )

    async def close(self) -> None:
        await self._client.aclose()


# --------------------------------------------------------------------------
# Serviço usado pelo grafo (protocolo DeliveryPublisher em app.graph.nodes)
# --------------------------------------------------------------------------


class GitHubDeliveryService:
    """Publica + espera CI e devolve um DeliveryResult. Credencial resolvida
    na hora da publicação (o worker pode viver dias; tokens de App expiram).
    Nunca levanta exceção para o grafo: erro vira DeliveryResult(error=...)."""

    def __init__(
        self,
        *,
        token_provider_factory: Callable[[], TokenProvider | None] = (
            build_token_provider_from_env
        ),
        client_factory: Callable[[], httpx.AsyncClient] | None = None,
        poll_interval_seconds: float = 15.0,
        grace_seconds: float = 90.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._token_provider_factory = token_provider_factory
        self._client_factory = client_factory
        self._poll_interval = poll_interval_seconds
        self._grace = grace_seconds
        self._sleep = sleep
        self._clock = clock

    async def publish(
        self,
        *,
        config: DeliveryConfig,
        workflow_id: str,
        project_id: str,
        files: list[dict[str, str]],
        deletions: list[str],
        summary: str,
    ) -> DeliveryResult:
        provider = self._token_provider_factory()
        if provider is None:
            return DeliveryResult(
                ci_state="error",
                error="Entrega configurada mas nenhuma credencial GitHub disponível "
                "(GITHUB_TOKEN ou GitHub App).",
                files=len(files),
                deletions=len(deletions),
            )
        client = GitHubSCMClient(
            token_provider=provider,
            client=self._client_factory() if self._client_factory else None,
        )
        head = config.head_branch or f"forgehand/{workflow_id[:12]}"
        result: DeliveryResult | None = None
        try:
            pull = await client.publish_pull_request(
                repository=config.repository,
                base_branch=config.base_branch,
                head_branch=head,
                title=config.title or f"Forgehand: {project_id}",
                body=(f"Entrega auditável do workflow `{workflow_id}`.\n\n{summary}"),
                files=files,
                deletions=deletions,
                commit_message=f"forgehand: {summary}",
                pinned_base_sha=config.pinned_base_sha,
                expected_head_sha=config.expected_head_sha,
            )
            result = DeliveryResult(
                pull_request_number=pull.number,
                url=pull.url,
                branch=pull.branch,
                commit_sha=pull.commit_sha,
                ci_state="skipped",
                files=len(files),
                deletions=len(deletions),
            )
            if not config.wait_for_checks:
                return result
            checks = await client.wait_for_checks(
                config.repository,
                pull.commit_sha,
                timeout_seconds=config.checks_timeout_seconds,
                poll_interval_seconds=self._poll_interval,
                grace_seconds=self._grace,
                sleep=self._sleep,
                clock=self._clock,
            )
            return result.model_copy(
                update={
                    "ci_state": checks.state,
                    "checks": checks.as_dicts(),
                    "failures": checks.failures,
                    "failure_paths": checks.failure_paths,
                }
            )
        except (SCMError, ValueError, httpx.HTTPError) as exc:
            if result is not None:
                return result.model_copy(
                    update={
                        "ci_state": "error",
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
            return DeliveryResult(
                ci_state="error",
                error=f"{type(exc).__name__}: {exc}",
                branch=head,
                commit_sha=config.expected_head_sha,
                files=len(files),
                deletions=len(deletions),
            )
        finally:
            await client.close()
