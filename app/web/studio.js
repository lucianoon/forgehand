(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  let current = null, busy = false, lastPayload = '', intakeKey = '';
  const names = {drafting:'Preparando escopo', approval_required:'Revise o escopo', building:'Construindo',
    ready_for_preview:'Pronto para experimentar', failed:'Precisa de atenção'};
  const errors = {insufficient_budget:'O orçamento restante não cobre a estimativa da próxima geração. Crie uma nova ideia com outro limite.',
    operation_interrupted:'A geração foi interrompida. O custo desconhecido ficou reservado; não houve repetição automática.',
    budget_exceeded:'O uso reportado ultrapassou o limite estimado. Verifique os limites do provedor.'};
  async function api(path, options = {}) {
    const response = await fetch('/products' + path, {...options, headers:{
      'Content-Type':'application/json', 'X-API-Key':$('studio-key').value, ...options.headers}});
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      throw new Error(typeof body.detail === 'string' ? body.detail : 'Revise os campos (' + response.status + ').');
    }
    return options.binary ? response.blob() : response.json();
  }
  function message(text) {$('studio-message').textContent = text;}
  async function action(callback) {
    if (busy) return;
    busy = true; document.querySelectorAll('button').forEach(button => {button.disabled = true;});
    try {await callback();} catch (error) {message(error.message);}
    finally {busy = false; document.querySelectorAll('button').forEach(button => {button.disabled = false;}); window.dispatchEvent(new Event('forgehand:idle'));}
  }
  async function history() {
    const items = await api('?project_id=' + encodeURIComponent($('studio-project').value));
    const list = $('studio-history'); list.replaceChildren();
    items.forEach(item => {
      const button = document.createElement('button'); button.className = 'history-item';
      button.textContent = (item.brief?.name || item.idea.slice(0,55)) + ' · ' + names[item.status];
      button.onclick = () => action(async () => render(await api('/' + item.id)));
      list.append(button);
    });
    if (!items.length) list.textContent = 'Sua primeira ideia começa no formulário acima.';
  }
  async function render(product) {
    current = product;
    window.dispatchEvent(new CustomEvent('forgehand:product', {detail:product}));
    $('studio-state').textContent = names[product.status] || product.status;
    $('studio-cost').textContent = '$' + product.cost_usd.toFixed(4) + ' usado · $' + product.reserved_usd.toFixed(4) + ' reservado';
    $('brief-form').hidden = product.status !== 'approval_required';
    $('demo-area').hidden = product.status !== 'ready_for_preview';
    $('demo-frame').srcdoc = '';
    const stage = product.status === 'ready_for_preview' ? 'demo' : product.brief ? 'brief' : 'idea';
    ['idea','brief','demo'].forEach(key => $('step-' + key).classList.toggle('active', key === stage));
    if (product.brief) Object.entries(product.brief).forEach(([key,value]) => {
      const input = $('brief-' + key); if (input) input.value = Array.isArray(value) ? value.join('\n') : value;
    });
    message(product.error ? (errors[product.error] || 'A geração falhou (' + product.error + '). O histórico foi preservado; confira a configuração do provedor.')
      : product.status === 'approval_required' ? 'Edite o que precisar. A construção só começa quando você aprovar.'
      : product.status === 'ready_for_preview' ? 'Use os formulários e valide os critérios abaixo. Esta demo não é uma certificação de software pronto para produção.'
      : 'Geração em andamento. Se a conexão cair, use Atualizar para recuperar o projeto.');
    if (product.status === 'ready_for_preview') {
      const preview = await api('/' + product.id + '/preview');
      // Mount a fresh frame after the panel is visible. Some embedded browsers
      // keep a document created under display:none without a rendered layout.
      const frame = $('demo-frame').cloneNode(false);
      frame.srcdoc = preview.document;
      $('demo-frame').replaceWith(frame);
      $('approved-brief').textContent = JSON.stringify(product.brief, null, 2);
      const list = $('demo-checklist'); list.replaceChildren();
      product.brief.acceptance_criteria.forEach(text => {
        const label = document.createElement('label'), checkbox = document.createElement('input');
        checkbox.type = 'checkbox'; label.append(checkbox, document.createTextNode(text)); list.append(label);
      });
    }
  }
  $('idea-form').onsubmit = event => {event.preventDefault(); action(async () => {
    const body = {project_id:$('studio-project').value.trim(), idea:$('studio-idea').value.trim(),
      audience:$('studio-audience').value.trim(), max_cost_usd:Number($('studio-budget').value)};
    const fingerprint = JSON.stringify(body);
    if (lastPayload !== fingerprint) {lastPayload = fingerprint; intakeKey = crypto.randomUUID();}
    message('Preparando um escopo para você revisar…');
    await render(await api('', {method:'POST', body:JSON.stringify({...body,idempotency_key:intakeKey})}));
    await history();
  });};
  $('brief-form').onsubmit = event => {event.preventDefault(); action(async () => {
    if (!current) return;
    const brief = {};
    ['name','audience','outcome'].forEach(key => {brief[key] = $('brief-' + key).value.trim();});
    ['features','backlog','acceptance_criteria','out_of_scope'].forEach(key => {
      brief[key] = $('brief-' + key).value.split('\n').map(line=>line.trim()).filter(Boolean);
    });
    message('Construindo a aplicação a partir do escopo aprovado…');
    await render(await api('/' + current.id + '/approve', {method:'POST', body:JSON.stringify({brief})}));
    await history();
  });};
  $('studio-refresh').onclick = () => action(async () => {await history(); if (current) await render(await api('/' + current.id));});
  $('download-demo').onclick = () => action(async () => {
    if (!current) return;
    const blob = await api('/' + current.id + '/download', {binary:true});
    const url = URL.createObjectURL(blob), link = document.createElement('a');
    link.href = url; link.download = 'forgehand-demo.zip'; link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  });
  $('download-fullstack').onclick = () => action(async () => {
    if (!current) return;
    const blob = await api('/' + current.id + '/fullstack', {binary:true});
    const url = URL.createObjectURL(blob), link = document.createElement('a');
    link.href = url; link.download = 'forgehand-fullstack.zip'; link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    message('Pacote com login e banco preparado. Siga o README para instalar; a prévia acima continua sendo a demo de sessão.');
  });
})();
