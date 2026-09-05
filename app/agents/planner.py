"""Planner — transforma requisição em plano de AgentTasks.

Decisões:
- O LLM referencia dependências por ÍNDICE, não por UUID: modelos não geram
  UUIDs confiáveis. A conversão índice→UUID acontece aqui, com validação de
  faixa e detecção de ciclo (Kahn) — plano cíclico nunca entra no grafo.
- Regra 3 no schema: acceptance_criteria com min_length=1. O planner é
  OBRIGADO pelo schema a definir critérios antes da execução existir.
- Tier configurável, default STANDARD (regra 9: caro só por escalonamento).
"""

from __future__ import annotations

import json
from pathlib import PurePosixPath
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel, Field, field_validator

from app.agents.grounding import (
    build_grounding_prefix,
    get_evidence_index,
    grounding_required,
)
from app.agents.tools import AgentTool, ToolLoop
from app.agents.web_tools import WEB_TOOL_GUIDANCE
from app.agents.hooks import ToolHookDispatcher
from app.models.task import (
    AcceptanceCriterion,
    AgentTask,
    Capability,
    CriterionKind,
    TaskBudget,
    coerce_criteria,
)
from app.graph.nodes import PlanningOutcome, UsageReport
from app.providers.base import CompletionRequest, Message
from app.providers.registry import ModelTier, ProviderRouter

SYSTEM_PROMPT = """Você é o planner do Forgehand, um sistema multiagente \
de desenvolvimento de software.

Decomponha a requisição em tarefas atômicas e executáveis. Para cada tarefa:
- title: curto e específico;
- description: o que fazer, com contexto suficiente para um executor \
independente que NÃO viu a requisição original;
- capability: exatamente uma competência;
- write_paths: caminhos relativos exatos dos arquivos que a tarefa pretende
  criar, editar ou remover; [] somente quando não grava arquivos. Declare também
  arquivos de testes. Não use globs. Essa declaração é verificada contra os
  critérios; não autoriza gravações nem substitui os gates de execução;
- acceptance_criteria: critérios verificáveis (mínimo 1). Cada critério tem \
`text` (o contrato legível) e `kind`. Prefira kinds OBJETIVOS, decididos por \
código sem margem de interpretação: tests_pass / lint_pass / types_pass \
(sinais pytest/ruff/mypy), file_created (path), file_modified (path), \
file_unchanged (path: "X não pode ser alterado"), no_existing_file_modified \
(SÓ para tarefas que criam arquivos novos sem tocar em nenhum existente — \
não use quando a tarefa edita um arquivo), changes_limited_to (paths, aceita \
globs), content_contains (path + pattern regex), citations_valid (análises \
grounded), output_contains (pattern regex sobre o texto entregue em \
summary/notes) e output_min_chars (min_chars) — estes dois para tarefas que \
NÃO gravam arquivo, como análise e pesquisa. \
Use `subjective` só para o que realmente exige julgamento (qualidade, \
clareza, aderência a um desenho). Um plano bom mistura os dois: o objetivo \
prova que a mudança aconteceu, o subjetivo julga se ficou boa;
- evidence_ids: IDs das evidências do repositório que justificam a tarefa; \
  quando houver grounding no contexto, cada tarefa DEVE citar pelo menos 1 \
  evidência real;
- depends_on: índices (base 0) das tarefas que precisam estar COMPLETAS \
antes desta iniciar. Use apenas dependências reais — tarefas independentes \
executam em paralelo;
- is_critical: true apenas para tarefas cujo erro compromete todo o projeto \
(ex.: arquitetura, segurança).

Prefira menos tarefas bem definidas a muitas tarefas vagas. Não crie tarefa \
de "revisão final" — o judge já existe.

Critérios devem expressar o resultado pedido, não uma implementação inventada:
- preservar API ou comportamento NÃO significa file_unchanged; uma refatoração
  altera o arquivo. Use tests_pass e um critério subjective de compatibilidade;
- file_unchanged exige que o arquivo inteiro permaneça byte a byte igual;
- content_contains só verifica presença de texto. Não prova comportamento,
  contagem de ocorrências ou equivalência. Não imponha uma expressão de código
  específica quando implementações equivalentes satisfazem o pedido;
- pattern contém somente uma regex Python, nunca JSON de outros critérios;
- mantenha separados os critérios de testes, escopo e comportamento.
- tests_pass/lint_pass/types_pass provam somente que a fase correspondente
  executou com sucesso; não provam cobertura de um caso específico. Não rotule
  "lista vazia retorna zero" como tests_pass: use um critério de comportamento
  e, quando a tarefa incluir regressão, outro que exija o teste desse caso;
- cada tarefa deve ser aprovável antes das tarefas que dependem dela. Para uma
  correção pequena, prefira implementar a mudança e seus testes de regressão na
  mesma tarefa. Se separar implementação e testes, atribua a criação/cobertura
  dos testes à tarefa responsável, sem exigir o trabalho futuro na antecessora.

Se o contexto trouxer evidências do repositório:
- use SOMENTE tecnologias, componentes e afirmações sustentadas por essas evidências;
- não invente arquivos, frameworks, serviços ou diretórios ausentes;
- quando a evidência for insuficiente, restrinja o plano ao que está demonstrado.

Para pedidos de análise do repositório/código:
- prefira no máximo 2 tarefas quando possível;
- use uma tarefa factual para mapear evidências e uma segunda, dependente da primeira,
  apenas se a síntese final realmente precisar ser separada;
- evite planos amplos que reavaliem a mesma evidência em múltiplas tarefas."""

TOOLS_GUIDANCE = """

Ferramentas disponíveis: read_file, list_directory e search_repository. \
Use-as só quando as evidências do grounding não bastarem para decidir a \
decomposição (ex.: confirmar se um módulo existe). Poucas chamadas, então \
emita o plano."""


class PlannedTask(BaseModel):
    title: str
    description: str
    capability: Capability
    acceptance_criteria: list[AcceptanceCriterion] = Field(min_length=1)
    write_paths: list[str] | None = Field(default=None, max_length=256)
    evidence_ids: list[str] = Field(default_factory=list)
    depends_on: list[int] = Field(default_factory=list)
    is_critical: bool = False

    @field_validator("acceptance_criteria", mode="before")
    @classmethod
    def _coerce_criteria(cls, value: Any) -> Any:
        # modelos que ainda devolvem strings: vira subjective (ou o kind
        # inferido por compatibilidade), sem quebrar o plano
        return coerce_criteria(value)


class PlanOutput(BaseModel):
    rationale: str = Field(description="Por que o plano foi dividido assim.")
    tasks: list[PlannedTask] = Field(min_length=1)


class PlanValidationError(ValueError):
    """Plano inválido — dependências, grounding ou critérios contraditórios."""


def _plan_path(value: str) -> str:
    path = PurePosixPath(value)
    if (
        not value or len(value) > 512 or str(path) == "." or path.is_absolute()
        or ".." in path.parts or any(c in value for c in "\\*?[]")
        or any(ord(c) < 32 for c in value)
    ):
        raise PlanValidationError("write_paths e critérios exigem caminhos relativos exatos, sem traversal ou globs.")
    return str(path)


def _task_stable_id(task: PlannedTask) -> UUID:
    fingerprint = json.dumps(
        {
            "title": task.title,
            "description": task.description,
            "capability": task.capability.value,
            "acceptance_criteria": [
                c.model_dump(mode="json") for c in task.acceptance_criteria
            ],
            "evidence_ids": task.evidence_ids,
            "depends_on": task.depends_on,
            "is_critical": task.is_critical,
            **({"write_paths": task.write_paths} if task.write_paths is not None else {}),
        },
        ensure_ascii=True,
        sort_keys=True,
    )
    return uuid5(NAMESPACE_URL, f"forgehand-plan:{fingerprint}")


def _to_agent_tasks(plan: PlanOutput) -> list[AgentTask]:
    n = len(plan.tasks)
    for i, t in enumerate(plan.tasks):
        bad = [d for d in t.depends_on if d < 0 or d >= n or d == i]
        if bad:
            raise PlanValidationError(
                f"Tarefa {i} ('{t.title}') com dependências inválidas: {bad}"
            )

    # Kahn: se não conseguimos ordenar topologicamente, há ciclo
    indegree = {i: len(t.depends_on) for i, t in enumerate(plan.tasks)}
    queue = [i for i, d in indegree.items() if d == 0]
    visited = 0
    dependents: dict[int, list[int]] = {i: [] for i in range(n)}
    for i, t in enumerate(plan.tasks):
        for d in t.depends_on:
            dependents[d].append(i)
    while queue:
        node = queue.pop()
        visited += 1
        for dep in dependents[node]:
            indegree[dep] -= 1
            if indegree[dep] == 0:
                queue.append(dep)
    if visited != n:
        raise PlanValidationError("Plano contém ciclo de dependências.")

    tasks = [
        AgentTask(
            id=_task_stable_id(t),
            title=t.title,
            description=t.description,
            capability=t.capability,
            acceptance_criteria=t.acceptance_criteria,
            evidence_ids=t.evidence_ids,
            is_critical=t.is_critical,
        )
        for t in plan.tasks
    ]
    for i, t in enumerate(plan.tasks):
        tasks[i].dependencies = [tasks[d].id for d in t.depends_on]
    return tasks


class LLMPlanner:
    """Implementa o protocolo Planner de app.graph.nodes."""

    def __init__(
        self,
        router: ProviderRouter,
        tier: ModelTier = ModelTier.STANDARD,
        escalate_on_retry: bool = True,
        default_task_budget: TaskBudget | None = None,
        max_validation_attempts: int = 2,
        tools: list[AgentTool] | None = None,
        max_tool_calls: int = 4,
        non_writing_capabilities: set[Capability] | None = None,
        apply_files_enabled: bool = True,
        require_write_paths: bool = False,
        hooks: ToolHookDispatcher | None = None,
    ):
        self._router = router
        self._tool_loop = ToolLoop(
            router,
            tools,
            max_tool_calls=max_tool_calls,
            hooks=hooks,
            agent_name="planner",
        )
        # Vem das execution strategies do container: capabilities cujo
        # resultado é só texto (apply_files=False). O planner precisa saber
        # para não exigir file_created/content_contains de quem não grava.
        self._non_writing = set(non_writing_capabilities or ())
        self._apply_files_enabled = apply_files_enabled
        self._require_write_paths = require_write_paths
        self._tier = tier
        self._escalate_on_retry = escalate_on_retry
        self._default_task_budget = default_task_budget or TaskBudget()
        self._max_validation_attempts = max(1, max_validation_attempts)

    def _system_prompt(self) -> str:
        prompt = SYSTEM_PROMPT
        if self._require_write_paths:
            prompt += "\n\nModo fábrica: write_paths é obrigatório em cada tarefa; null não é aceito."
        if not self._apply_files_enabled:
            prompt += (
                "\n\nNesta execução NENHUMA tarefa grava arquivos no workspace: o "
                "resultado de cada tarefa é o texto de `summary`/`notes`. Não use "
                "critérios de arquivo (file_created, file_modified, content_contains, "
                "changes_limited_to); use output_contains (pattern) e output_min_chars "
                "(min_chars) para provar o entregável, e subjective para a qualidade."
            )
        elif self._non_writing:
            names = ", ".join(sorted(c.value for c in self._non_writing))
            prompt += (
                f"\n\nCapabilities que NÃO gravam arquivos ({names}): o resultado é o "
                "texto de `summary`/`notes`. Nelas, não use critérios de arquivo "
                "(file_created, file_modified, content_contains, changes_limited_to); "
                "use output_contains (pattern) e output_min_chars (min_chars). "
                "Para produzir um documento no repositório use `documentation`."
            )
        if self._tool_loop.has_tools:
            prompt += TOOLS_GUIDANCE
            if self._tool_loop.has_tool("fetch_url"):
                prompt += WEB_TOOL_GUIDANCE
        return prompt

    @staticmethod
    def _non_grounding_context(context: dict[str, Any]) -> dict[str, Any]:
        return {
            key: value
            for key, value in context.items()
            if key not in ("repository_grounding", "web_references")
        }

    @staticmethod
    def _is_repository_analysis(request: str, context: dict[str, Any]) -> bool:
        lowered = request.lower()
        return "repository_grounding" in context and any(
            token in lowered
            for token in (
                "reposit",
                "repo",
                "codebase",
                "código",
                "arquitet",
                "módulo",
            )
        )

    @staticmethod
    def _validate_grounding(plan: PlanOutput, context: dict[str, Any]) -> None:
        if not grounding_required(context):
            return

        evidence_index = get_evidence_index(context)
        for task in plan.tasks:
            if not task.evidence_ids:
                raise PlanValidationError(
                    f"Tarefa '{task.title}' sem evidence_ids em modo grounded."
                )
            invalid = [
                evidence_id
                for evidence_id in task.evidence_ids
                if evidence_id not in evidence_index
            ]
            if invalid:
                raise PlanValidationError(
                    f"Tarefa '{task.title}' referencia evidências inexistentes: {invalid}"
                )

    def _apply_defaults(self, tasks: list[AgentTask]) -> list[AgentTask]:
        return [
            task.model_copy(
                update={
                    "budget": task.budget.model_copy(
                        update={
                            "max_tokens": self._default_task_budget.max_tokens,
                            "max_cost_usd": self._default_task_budget.max_cost_usd,
                        }
                    )
                }
            )
            for task in tasks
        ]

    def _validate_plan_consistency(self, plan: PlanOutput) -> None:
        for task in plan.tasks:
            if self._require_write_paths and task.write_paths is None:
                raise PlanValidationError(
                    f"Tarefa '{task.title}' sem write_paths. Declare os arquivos a alterar; [] para tarefa somente leitura."
                )
            writes = {_plan_path(path) for path in task.write_paths or []}
            # Explicit objective edit requirements are also declarations, including
            # legacy plans without write_paths. Check only this task's contract:
            # a different task may legitimately edit the same file later.
            writes.update(
                _plan_path(criterion.path)
                for criterion in task.acceptance_criteria
                if criterion.kind in {CriterionKind.FILE_CREATED, CriterionKind.FILE_MODIFIED}
                and criterion.path is not None
            )
            protected = {
                _plan_path(criterion.path)
                for criterion in task.acceptance_criteria
                if criterion.kind == CriterionKind.FILE_UNCHANGED and criterion.path is not None
            }
            conflicts = sorted(writes & protected)
            if conflicts:
                raise PlanValidationError(
                    f"Tarefa '{task.title}' contraditória: write_paths/file_modified/file_created "
                    f"e file_unchanged para {conflicts}. Preservar funções/API não significa "
                    "congelar o arquivo inteiro. Refaça o plano conforme o pedido original, "
                    "sem remover proteções realmente exigidas pelo usuário."
                )

    def _tier_for_attempt(self, attempt: int) -> ModelTier:
        """Replanejamento após rejeição estrutural sobe um tier (regra 8: caro
        só por escalonamento), sem passar de STRONG."""
        if attempt <= 1 or not self._escalate_on_retry:
            return self._tier
        return ModelTier(min(int(self._tier) + 1, int(ModelTier.STRONG)))

    async def create_plan(
        self, request: str, context: dict[str, Any]
    ) -> PlanningOutcome:
        user_content = f"Requisição:\n{request}"
        extra_context = self._non_grounding_context(context)
        if extra_context:
            user_content += f"\n\nContexto adicional do projeto:\n{extra_context}"
        cache_prefix = build_grounding_prefix(context)
        if self._is_repository_analysis(request, context):
            user_content += (
                "\n\nDiretriz de economia para análise grounded do repositório:\n"
                "- prefira 1 tarefa factual ou no máximo 2 tarefas;\n"
                "- não replique a mesma análise em capabilities diferentes sem necessidade;\n"
                "- use a segunda tarefa apenas para sintetizar um artefato final dependente da primeira."
            )

        total_tokens = 0
        total_cost = 0.0
        validation_feedback = ""
        for attempt in range(1, self._max_validation_attempts + 1):
            attempt_content = user_content
            if validation_feedback:
                attempt_content += (
                    "\n\nO plano anterior foi rejeitado pela validação estrutural:\n"
                    f"{validation_feedback}\n"
                    "Gere o plano completo novamente, corrigindo índices, ciclos e "
                    "evidence_ids, write_paths e contradições de critérios."
                )
            loop_outcome = await self._tool_loop.run(
                self._tier_for_attempt(attempt),
                CompletionRequest(
                    model="",  # resolvido pelo router
                    cache_prefix=cache_prefix,
                    system=self._system_prompt(),
                    messages=[Message(role="user", content=attempt_content)],
                    response_schema=PlanOutput,
                    max_tokens=8192,
                ),
            )
            total_tokens += loop_outcome.tokens
            total_cost += loop_outcome.cost_usd
            parsed = loop_outcome.result.parse_as(PlanOutput)
            try:
                self._validate_grounding(parsed, context)
                self._validate_plan_consistency(parsed)
                tasks = _to_agent_tasks(parsed)
            except PlanValidationError as exc:
                if attempt == self._max_validation_attempts:
                    raise
                validation_feedback = str(exc)
                continue
            return PlanningOutcome(
                plan=self._apply_defaults(tasks),
                usage=UsageReport(tokens=total_tokens, cost_usd=total_cost),
            )
        raise AssertionError("loop de validação do planner terminou sem resultado")
