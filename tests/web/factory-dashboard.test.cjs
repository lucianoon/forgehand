const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

function dashboard() {
  const elements = new Map();
  const requests = [];
  const element = id => {
    if (!elements.has(id)) elements.set(id, {
      value: '', checked: true, textContent: '', innerHTML: '', dataset: {}, listeners: {},
      classList: {toggle() {}, add() {}},
      addEventListener(event, callback) { this.listeners[event] = callback; },
      append(child) { this.child = child; },
    });
    return elements.get(id);
  };
  const context = vm.createContext({
    URL, Intl, console,
    document: {getElementById: element, querySelector: element, querySelectorAll: () => [],
      createElement() { return {value: '', set textContent(value) { this.value = String(value); }, get innerHTML() { return this.value.replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('"', '&quot;'); }}; }},
    window: {setInterval() {}, clearInterval() {}, setTimeout() {}},
    sessionStorage: {removeItem() {}},
    fetch: async (url, options = {}) => {
      requests.push({url, ...options});
      return {ok: true, json: async () => url === '/workflows' && options.method === 'POST' ? {workflow_id: 'wf'} : url.startsWith('/workflows?') ? [] : {workflow_id: 'wf', status: 'running', tasks: [], usage: {}}};
    },
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../../app/web/app.js'), 'utf8'), context);
  return {element, requests, context};
}

for (const kind of ['direct', 'issue', 'legacy']) test(`submits ${kind} through workflow API`, async () => {
  const {element, requests} = dashboard();
  for (const [id, value] of Object.entries({'work-kind': kind, 'api-key': 'SECRET', 'project-id': 'demo', request: 'Corrigir total do pedido', criteria: 'Testes passam\nSem regressões', 'factory-repository': 'acme/r', 'issue-url': 'https://github.com/acme/r/issues/7', 'base-ref': 'main', 'max-tokens': '10000', 'max-cost': '1', 'max-iterations': '2', 'max-seconds': '300', 'checks-timeout': '60'})) element(id).value = value;
  element('work-kind').listeners.change();
  await element('workflow-form').listeners.submit({preventDefault() {}});
  const sent = requests.find(r => r.method === 'POST');
  const body = JSON.parse(sent.body);
  assert.equal(sent.headers['X-API-Key'], 'SECRET');
  assert.equal(sent.body.includes('SECRET'), false);
  assert.equal(body.project_id, 'demo');
  if (kind === 'legacy') assert.equal(body.request, 'Corrigir total do pedido');
  else {
    assert.equal(body.request, undefined);
    assert.equal(body.work_order.limits.max_wall_clock_seconds, 300);
    assert.equal(body.work_order.delivery_policy.require_human_merge, true);
    if (kind === 'issue') assert.equal(body.work_order.issue_url, 'https://github.com/acme/r/issues/7');
    else assert.equal(body.work_order.repository, 'acme/r');
  }
});

test('renders bounded evidence and rejects unsafe PR links', () => {
  const {context, element} = dashboard();
  context.state = {provenance: {source: {snapshot: {number: 1, title: '<script>bad</script>'}}, repository: {}, limits: {}}, workspace: {base_sha: 'a'.repeat(40), state: 'retained'}, phase_evidence: {outcome: 'command_failure', phases: [{phase: 'test', outcome: 'command_failure', stdout: 'SECRET'.repeat(100000)}]}, delivery: {url: 'javascript:alert(1)'}};
  vm.runInContext('renderFactory(state)', context);
  assert.ok(element('factory-facts').innerHTML.includes('&lt;script>'));
  assert.ok(element('factory-facts').innerHTML.includes('a'.repeat(40)));
  assert.ok(!element('phase-evidence').innerHTML.includes('SECRET'));
  assert.equal(element('factory-pr').child, undefined);
});
