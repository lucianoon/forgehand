const $ = (id) => document.getElementById(id);
const stages = ["queued", "loading_context", "planning", "executing", "evaluating", "replanning", "synthesizing", "persisting", "completed"];
let workflowId = null;
let pollTimer = null;
let factoryRun = false;

const formatNumber = (value) => new Intl.NumberFormat("pt-BR").format(value || 0);
const formatCost = (value) => new Intl.NumberFormat("pt-BR", { style: "currency", currency: "USD", minimumFractionDigits: 4 }).format(value || 0);
const apiHeaders = () => ({ "X-API-Key": $("api-key").value.trim(), "Content-Type": "application/json" });

async function refreshHealth() {
  try {
    const [readyResponse, metricsResponse] = await Promise.all([fetch("/readyz"), fetch("/metrics")]);
    const ready = await readyResponse.json();
    const metrics = await metricsResponse.json();
    $("runtime-dot").classList.toggle("ok", ready.ready);
    $("runtime-label").textContent = ready.ready ? "Runtime operacional" : "Runtime indisponível";
    $("queue-value").textContent = metrics.queue?.backend || "—";
    const visibleWorkers = metrics.workers?.registered || metrics.workers?.running || 0;
    $("workers-value").textContent = `${visibleWorkers}/${metrics.workers?.configured_concurrency || 0}`;
    $("active-value").textContent = metrics.workflow_runtime?.active_workflows || 0;
  } catch {
    $("runtime-label").textContent = "Sem conexão com o runtime";
  }
}

function setStage(stage) {
  const normalized = stage === "awaiting_human"
    ? "evaluating"
    : ["synthesizing", "persisting", "ready_for_human_review"].includes(stage) ? "completed" : stage;
  const current = stages.indexOf(normalized);
  document.querySelectorAll("#stage-track li").forEach((item) => {
    const target = stages.indexOf(item.dataset.stage);
    item.classList.toggle("active", item.dataset.stage === normalized);
    item.classList.toggle("done", current > target || normalized === "completed");
  });
}

function renderTasks(tasks) {
  $("task-count").textContent = `${tasks.length} ${tasks.length === 1 ? "tarefa" : "tarefas"}`;
  if (!tasks.length) {
    $("tasks").className = "tasks empty-state";
    $("tasks").innerHTML = "<p>As tarefas aparecerão aqui após o planejamento.</p>";
    return;
  }
  $("tasks").className = "tasks";
  $("tasks").innerHTML = tasks.map((task) => `
    <article class="task-card">
      <strong>${escapeHtml(task.title)}</strong><span class="task-status">${escapeHtml(task.status)}</span>
      <p>${escapeHtml(task.capability)} · ${task.attempts} ${task.attempts === 1 ? "tentativa" : "tentativas"}</p>
    </article>`).join("");
}

function escapeHtml(value) {
  const node = document.createElement("span");
  node.textContent = String(value ?? "");
  return node.innerHTML;
}

function renderState(state) {
  factoryRun = Boolean(state.provenance);
  renderFactory(state);
  document.querySelector(".publish-row").classList.toggle("hidden", factoryRun);
  $("workflow-id").textContent = state.workflow_id;
  $("run-badge").textContent = state.status === "ready_for_human_review" ? "Pronto para revisão humana" : state.status;
  $("run-badge").className = `badge ${state.status}`;
  $("tokens").textContent = formatNumber(state.usage?.tokens);
  $("cost").textContent = formatCost(state.usage?.cost_usd);
  $("iteration").textContent = state.iteration || 0;
  setStage(state.current_stage);
  renderTasks(state.tasks || []);
  $("decision").classList.toggle("hidden", state.status !== "awaiting_decision");
  const allowedDecisions = state.pending_decision?.options;
  document.querySelectorAll("[data-decision]").forEach((button) => {
    button.disabled = Array.isArray(allowedDecisions) && !allowedDecisions.includes(button.dataset.decision);
  });
  $("cancel-workflow").classList.toggle("hidden", !["running", "awaiting_decision"].includes(state.status));
  const hasOutput = Boolean(state.final_output);
  $("delivery").classList.toggle("hidden", !hasOutput);
  if (hasOutput) $("final-output").textContent = state.final_output;
  if (["completed", "ready_for_human_review", "failed", "cancelled"].includes(state.status)) stopPolling();
}

async function refreshDetails() {
  if (!workflowId) return;
  const response = await fetch(`/workflows/${workflowId}/details`, { headers: apiHeaders() });
  if (!response.ok) return;
  const details = await response.json();
  if (factoryRun) {
    $("operations").classList.add("hidden");
    return;
  }
  const operations = [];
  for (const task of details.tasks || []) {
    const workspace = task.result?.workspace;
    for (const diff of workspace?.file_diffs || []) {
      operations.push({ title: `${diff.change_type}: ${diff.path}`, content: diff.diff || "Sem alteração textual." });
    }
    for (const command of workspace?.command_executions || []) {
      operations.push({ title: `${command.passed ? "✓" : "×"} ${command.name} · exit ${command.exit_code}`, content: command.stdout || command.stderr || command.command });
    }
  }
  $("operations").classList.toggle("hidden", operations.length === 0);
  $("operation-count").textContent = `${operations.length} registros`;
  $("operation-list").innerHTML = operations.map((operation) => `
    <article class="operation"><strong>${escapeHtml(operation.title)}</strong><pre>${escapeHtml(operation.content)}</pre></article>`).join("");
}

async function pollWorkflow() {
  if (!workflowId) return;
  try {
    const response = await fetch(`/workflows/${workflowId}`, { headers: apiHeaders() });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.detail || "Não foi possível consultar o workflow.");
    renderState(payload);
    await refreshDetails();
    await refreshHealth();
  } catch (error) {
    $("form-error").textContent = error.message;
  }
}

function startPolling() {
  stopPolling();
  pollWorkflow();
  pollTimer = window.setInterval(pollWorkflow, 1000);
}
function stopPolling() { if (pollTimer) window.clearInterval(pollTimer); pollTimer = null; }

async function refreshHistory() {
  if (!$('api-key').value.trim()) return;
  try {
    const project = $('project-id').value.trim();
    const query = project ? `?limit=10&project_id=${encodeURIComponent(project)}` : '?limit=10';
    const response = await fetch(`/workflows${query}`, { headers: apiHeaders() });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.detail || 'Falha ao carregar histórico.');
    $('workflow-history').className = payload.length ? 'workflow-history' : 'workflow-history empty-history';
    $('workflow-history').innerHTML = payload.length ? payload.map((item) => `
      <button class="history-item" type="button" data-workflow-id="${escapeHtml(item.workflow_id)}">
        <strong>${escapeHtml(item.workflow_id)}</strong><span>${escapeHtml(item.status)}</span>
        <small>${formatNumber(item.usage?.tokens)} tokens · ${formatCost(item.usage?.cost_usd)}</small>
      </button>`).join('') : 'Nenhuma execução encontrada para este projeto.';
    document.querySelectorAll('[data-workflow-id]').forEach((button) => button.addEventListener('click', () => {
      workflowId = button.dataset.workflowId;
      startPolling();
    }));
  } catch (error) {
    $('workflow-history').className = 'workflow-history empty-history';
    $('workflow-history').textContent = error.message;
  }
}

$("workflow-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  $("form-error").textContent = "";
  $("submit-button").disabled = true;
  const criteria = $("criteria").value.split("\n").map((item) => item.trim()).filter(Boolean);
  const body = {
    project_id: $("project-id").value.trim(),
    request: $("request").value.trim(),
    acceptance_criteria: criteria,
    budget: { max_tokens: Number($("max-tokens").value), max_cost_usd: Number($("max-cost").value) },
  };
  if ($("work-kind").value !== "legacy") {
    delete body.request;
    delete body.acceptance_criteria;
    delete body.budget;
    body.work_order = {
      base_ref: $("base-ref").value.trim(),
      acceptance_criteria: criteria,
      build_profile: $("build-profile").value.trim() || null,
      limits: { max_tokens: Number($("max-tokens").value), max_cost_usd: Number($("max-cost").value), max_iterations: Number($("max-iterations").value), max_wall_clock_seconds: Number($("max-seconds").value) },
      delivery_policy: { wait_for_checks: $("wait-checks").checked, checks_timeout_seconds: Number($("checks-timeout").value), require_human_merge: true },
    };
    if ($("work-kind").value === "issue") body.work_order.issue_url = $("issue-url").value.trim();
    else Object.assign(body.work_order, { repository: $("factory-repository").value.trim(), requested_outcome: $("request").value.trim() });
  }
  try {
    const response = await fetch("/workflows", { method: "POST", headers: apiHeaders(), body: JSON.stringify(body) });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.detail?.[0]?.msg || payload.detail || "Falha ao iniciar workflow.");
    workflowId = payload.workflow_id;
    $("workflow-id").textContent = workflowId;
    startPolling();
    await refreshHistory();
  } catch (error) {
    $("form-error").textContent = error.message;
  } finally {
    $("submit-button").disabled = false;
  }
});

document.querySelectorAll("[data-decision]").forEach((button) => button.addEventListener("click", async () => {
  button.disabled = true;
  try {
    const response = await fetch(`/workflows/${workflowId}/decision`, { method: "POST", headers: apiHeaders(), body: JSON.stringify({ decision: button.dataset.decision }) });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.detail || "Falha ao registrar decisão.");
    $("decision").classList.add("hidden");
    startPolling();
  } catch (error) {
    $("form-error").textContent = error.message;
  } finally { button.disabled = false; }
}));

$("cancel-workflow").addEventListener("click", async () => {
  if (!workflowId) return;
  $("cancel-workflow").disabled = true;
  try {
    const response = await fetch(`/workflows/${workflowId}/cancel`, { method: "POST", headers: apiHeaders() });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.detail || "Falha ao cancelar workflow.");
    await pollWorkflow();
  } catch (error) {
    $("form-error").textContent = error.message;
  } finally { $("cancel-workflow").disabled = false; }
});

$("copy-output").addEventListener("click", async () => {
  await navigator.clipboard.writeText($("final-output").textContent);
  $("copy-output").textContent = "Copiado";
  window.setTimeout(() => $("copy-output").textContent = "Copiar", 1200);
});

$("publish-pr").addEventListener("click", async () => {
  const repository = $("github-repository").value.trim();
  if (!repository) { $("publish-result").textContent = "Informe owner/repository."; return; }
  $("publish-pr").disabled = true;
  try {
    const response = await fetch(`/workflows/${workflowId}/pull-request`, {
      method: "POST", headers: apiHeaders(), body: JSON.stringify({ repository }),
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.detail || "Falha ao publicar PR.");
    $("publish-result").innerHTML = `PR #${payload.number}: <a href="${escapeHtml(payload.url)}" target="_blank" rel="noreferrer">abrir no GitHub</a>`;
  } catch (error) {
    $("publish-result").textContent = error.message;
  } finally { $("publish-pr").disabled = false; }
});

// Credentials stay in the password control, not browser storage.
sessionStorage.removeItem("forgehand-api-key");
$('work-kind').addEventListener('change', () => {
  const factory = $('work-kind').value !== 'legacy';
  const issue = $('work-kind').value === 'issue';
  $('factory-fields').disabled = !factory;
  $('factory-fields').classList.toggle('hidden', !factory);
  $('repository-label').classList.toggle('hidden', issue);
  $('issue-label').classList.toggle('hidden', !issue);
  $('factory-repository').required = factory && !issue;
  $('issue-url').required = issue;
  $('issue-url').disabled = !issue;
  $('request-label').classList.toggle('hidden', issue);
  $('request').required = !issue;
  $('request').disabled = issue;
  $('criteria').required = factory;
});
$('refresh-history').addEventListener('click', refreshHistory);
$('api-key').addEventListener('change', refreshHistory);
$('project-id').addEventListener('change', refreshHistory);
refreshHealth();
refreshHistory();
window.setInterval(refreshHealth, 5000);

function renderFactory(state) {
  const order = state.provenance;
  $('factory-status').classList.toggle('hidden', !order);
  if (!order) return;
  const lease = state.workspace || {};
  const strategy = state.build_strategy || {};
  const report = state.phase_evidence;
  const source = order.source?.snapshot;
  const facts = [
    ['Origem', source ? `Issue #${source.number} · ${source.title}` : 'Solicitação direta'],
    ['Repositório', order.repository?.full_name], ['Base', order.repository?.base_ref],
    ['SHA fixado', lease.base_sha || 'Aguardando provisionamento'],
    ['Workspace', lease.state || 'Aguardando'],
    ['Retenção até', lease.retention?.retain_until || 'Sem retenção registrada'],
    ['Perfil / motivo', `${strategy.selected_profile || 'Aguardando'} · ${strategy.selection_reason || '—'}`],
    ['Orçamento', `${formatNumber(order.limits?.max_tokens)} tokens · ${formatCost(order.limits?.max_cost_usd)} · ${order.limits?.max_wall_clock_seconds}s`],
    ['CI', state.delivery?.ci_state || 'Não publicado'],
  ];
  $('factory-facts').innerHTML = facts.map(([label, value]) => `<dt>${escapeHtml(label)}</dt><dd>${escapeHtml(value || '—')}</dd>`).join('');
  $('factory-phase').textContent = state.active_phase?.outcome === 'running' ? `Em validação: ${state.active_phase.phase}` : state.factory_stage || state.current_stage;
  const messages = {
    unsupported_build_strategy: 'Configure um perfil aprovado para este repositório e tente novamente.',
    provisioning: 'Preparando o checkout isolado. Em caso de falha, verifique acesso ao repositório e branch base.',
    cleanup: 'Limpando recursos locais; branches e PRs remotos serão preservados.',
    cancelled: 'Execução cancelada. Consulte a retenção e a limpeza do workspace.',
  };
  $('factory-action').textContent = state.next_human_action || messages[state.current_stage] ||
    (report && report.outcome !== 'success' ? `Validação interrompida: ${report.error_code || report.outcome}. Verifique o perfil, a política de preparação e a disponibilidade do Docker antes de tentar novamente.` : '') ||
    (state.pending_decision ? 'Verifique CI, tentativas e orçamento antes de tentar novamente ou abortar.' : messages[state.factory_stage]) ||
    (state.error ? 'A execução falhou. Verifique acesso, perfil e infraestrutura; os detalhes estão na auditoria.' : 'Acompanhe as validações. Nenhum merge será feito automaticamente.');
  $('phase-evidence').innerHTML = (report?.phases || []).slice(0, 5).map((phase) => `<article class="operation"><strong>${escapeHtml(phase.phase)} · ${escapeHtml(phase.outcome)}</strong><p>Exit ${escapeHtml(phase.exit_code ?? '—')} · ${escapeHtml(phase.duration_seconds)}s · ${phase.output_truncated ? 'saída truncada' : 'saída limitada'}</p><p>${escapeHtml(phase.error_code || '')}</p></article>`).join('');
  $('factory-pr').textContent = '';
  try {
    const url = new URL(state.delivery?.url);
    if (url.protocol === 'https:' && url.hostname === 'github.com' && !url.username && !url.password) {
      const link = document.createElement('a');
      link.href = url.href; link.target = '_blank'; link.rel = 'noopener noreferrer';
      link.textContent = `Revisar PR #${state.delivery.pull_request_number} · ${state.delivery.commit_sha || ''}`;
      $('factory-pr').append(link);
    }
  } catch { /* No verified PR URL yet. */ }
}
