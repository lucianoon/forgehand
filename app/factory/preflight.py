"""Local dispatch prerequisites, without SCM, model calls or repository execution."""

from __future__ import annotations

import asyncio
import os
from typing import Any, Literal, Protocol

from app.factory.build_strategy import BuildProfileRegistry
from app.factory.product_delivery import context_capsule
from app.infrastructure.product_delivery_store import next_feature
from app.infrastructure.product_store import ProductConflict
from app.infrastructure.settings import Settings
from app.models.product_delivery import DeliveryPreflight, DeliveryPreflightCheck

HEALTH_TIMEOUT_SECONDS = 2.0


class ReadinessSource(Protocol):
    async def readiness(self) -> dict[str, Any]: ...


def github_credential_configured() -> bool:
    """Presence only: do not open private-key paths or request an installation token."""
    return bool(os.environ.get("GITHUB_TOKEN")) or bool(
        os.environ.get("GITHUB_APP_ID")
        and os.environ.get("GITHUB_APP_INSTALLATION_ID")
        and (
            os.environ.get("GITHUB_APP_PRIVATE_KEY")
            or os.environ.get("GITHUB_APP_PRIVATE_KEY_PATH")
        )
    )


async def delivery_preflight(
    plan: dict[str, Any],
    settings: Settings,
    workflows: ReadinessSource,
    *,
    recovering: bool = False,
) -> DeliveryPreflight:
    checks: list[DeliveryPreflightCheck] = []

    def add(
        code: str, status: Literal["pass", "block", "warning"], message: str
    ) -> None:
        checks.append(DeliveryPreflightCheck(code=code, status=status, message=message))

    feature = next_feature(plan)
    eligible = feature is not None and feature["status"] in (
        {"dispatching", "dispatch_unknown"} if recovering
        else {"pending", "failed", "cancelled"}
    )
    add(
        "delivery_state",
        "pass" if eligible else "block",
        "Entrega disponível para autorização."
        if eligible
        else "Reconcilie a entrega ativa; se todas terminaram, acrescente outra entrega.",
    )
    attempts_ok = feature is not None and (
        bool(feature["attempts"]) if recovering else len(feature["attempts"]) < 3
    )
    add(
        "attempt_limit",
        "pass" if attempts_ok else "block",
        ("Recuperação usa a mesma tentativa e os limites salvos." if recovering else "Há tentativa disponível.")
        if attempts_ok
        else "Não há tentativa disponível; o limite é três por entrega.",
    )
    try:
        if not recovering:
            context_capsule(plan)
    except ProductConflict:
        add(
            "context",
            "block",
            "Contexto indisponível ou acima de 48 KB; revise o escopo antes de executar.",
        )
    else:
        add("context", "pass", "Recuperação usa a ordem imutável salva." if recovering
            else "Contexto da próxima entrega dentro do limite de 48 KB.")

    for code, passed, success, failure in [
        (
            "factory_enabled",
            settings.factory_mode_enabled,
            "Fábrica habilitada.",
            "Habilite FACTORY_MODE_ENABLED no servidor.",
        ),
        (
            "scm_host",
            any(host == "github.com" for host in settings.factory_approved_scm_hosts),
            "Host GitHub aprovado.",
            "Inclua github.com nos hosts aprovados da fábrica.",
        ),
        (
            "sandbox_policy",
            settings.factory_command_backend == "docker",
            "Política exige execução isolada em Docker.",
            "Configure o backend Docker; execução local não é aceita pela fábrica.",
        ),
        (
            "github_credential",
            github_credential_configured(),
            "Credencial GitHub configurada; acesso remoto não verificado.",
            "Configure uma credencial GitHub no servidor; não cole a chave no plano.",
        ),
    ]:
        add(code, "pass" if passed else "block", success if passed else failure)

    try:
        profiles = settings.factory_build_profiles
        mappings = settings.factory_repository_profiles
        BuildProfileRegistry(profiles, mappings)
        selected = plan["build_profile"] or {
            repo.casefold(): name for repo, name in mappings.items()
        }.get(plan["repository"].casefold())
        if selected is not None:
            profile = profiles.get(selected)
            if profile is None:
                add(
                    "build_profile",
                    "block",
                    "Perfil solicitado não aprovado; configure esse perfil no servidor.",
                )
            elif not settings.factory_sandbox_network_enabled and any(
                phase.network == "dependencies" for phase in profile.phases
            ):
                add(
                    "build_profile",
                    "block",
                    "O perfil exige rede para dependências, mas essa permissão está desativada no servidor.",
                )
            else:
                add(
                    "build_profile",
                    "pass",
                    "Perfil solicitado ou mapeado está aprovado na configuração local.",
                )
        elif any(profile.auto_detect for profile in profiles.values()):
            add(
                "build_profile",
                "warning",
                "Há perfil para detecção automática; compatibilidade e escolha só serão verificadas após o checkout.",
            )
        else:
            add(
                "build_profile",
                "block",
                "Configure um perfil explícito, mapeado ou com detecção automática aprovada.",
            )
    except ValueError:
        add(
            "build_profile",
            "block",
            "Configuração de perfis inválida; revise os perfis e mapeamentos no servidor.",
        )

    try:
        async with asyncio.timeout(HEALTH_TIMEOUT_SECONDS):
            health = await workflows.readiness()
        queue_ok = health.get("queue_ready") is True
        supported_workers = (
            health.get("embedded_workers_enabled") is True
            or settings.workflow_queue_backend == "postgres"
        )
        workers_ok = supported_workers and health.get("ready") is True
        add(
            "queue",
            "pass" if queue_ok else "block",
            "Fila respondeu à checagem."
            if queue_ok
            else "Fila indisponível; restaure a conexão antes de executar.",
        )
        add(
            "workers",
            "pass" if workers_ok else "block",
            "Workers disponíveis segundo o serviço de execução."
            if workers_ok
            else "Nenhum conjunto de workers pronto; inicie workers internos ou workers registrados com fila PostgreSQL.",
        )
    except Exception:
        add(
            "runtime_health",
            "block",
            "Não foi possível confirmar fila e workers no prazo; verifique o serviço e tente checar novamente.",
        )

    return DeliveryPreflight(
        product_id=plan["product_id"],
        revision=plan["revision"],
        checks=checks,
        not_checked=[
            "Acesso GitHub e branch: consultados no início da entrega; permissão de escrita só é exercida na publicação.",
            "Disponibilidade, saldo e limites do provedor de IA: nenhuma chamada de modelo foi feita.",
            "Docker, imagens, conteúdo do repositório e configuração efetiva de workers externos: não inspecionados aqui.",
        ],
    )
