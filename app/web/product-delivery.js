/* Durable delivery ledger. No automatic start, polling, merge or credentials in storage. */
(() => {
  const $ = id => document.getElementById(id);
  const names = {pending:'Aguardando execução', dispatching:'Envio registrado', running:'Em execução',
    dispatch_unknown:'Envio incerto · investigar workflow', awaiting_review:'Aguardando merge verificado',
    awaiting_decision:'Decisão necessária no painel', merged:'Merge verificado',
    failed:'Execução falhou', cancelled:'Cancelada', blocked:'Sem evidência de entrega'};
  let product = null, plan = null, enabled = false, busy = false, generation = 0, preflight = null;
  const lines = id => $(id).value.split('\n').map(x=>x.trim()).filter(Boolean);
  function reviewedFeatures(id) {
    const features = JSON.parse($(id).value);
    if (!Array.isArray(features)) throw new Error('As entregas devem ser uma lista JSON.');
    if (features.some(f=>f.acceptance_criteria?.some(text=>typeof text === 'string' && text.startsWith('Definir um critério específico e verificável para:')))) {
      throw new Error('Substitua os critérios provisórios por resultados específicos e verificáveis antes de salvar.');
    }
    return features;
  }
  function message(text) { $('delivery-message').textContent = text; }
  function clearPreflight() {
    preflight=null;
    $('delivery-preflight-checks').replaceChildren();
    $('delivery-preflight-summary').textContent='Checagem local pendente. Ela não inicia IA nem acessa o GitHub; o servidor repete as verificações ao executar.';
    $('delivery-preflight-limits').textContent='';
  }
  function showPreflight(report) {
    if (!plan || report.product_id !== product.id || report.revision !== plan.revision) {
      clearPreflight(); throw new Error('Plano alterado; atualize e refaça a checagem.');
    }
    preflight=report;
    $('delivery-approved').checked=false;
    $('delivery-preflight-summary').textContent=(report.can_start ? 'Sem bloqueios locais conhecidos' : 'Execução bloqueada')+' · revisão '+report.revision+'. Isto não garante sucesso da execução.';
    const list=$('delivery-preflight-checks'); list.replaceChildren();
    const labels={pass:'Verificado',block:'Bloqueio',warning:'Atenção'};
    report.checks.forEach(check=>{const li=document.createElement('li');li.textContent=(labels[check.status] || 'Atenção')+': '+check.message;list.append(li);});
    $('delivery-preflight-limits').textContent='Ainda não verificado: '+report.not_checked.join(' ');
    availability();
  }
  async function api(suffix='', body, method='GET') {
    const id = product.id, key = $('studio-key').value, token=generation;
    const response = await fetch('/products/' + encodeURIComponent(id) + '/delivery' + suffix, {
      method, headers:{'X-API-Key':key,'Content-Type':'application/json'},
      ...(body !== undefined ? {body:JSON.stringify(body)} : {})
    });
    if (id !== product?.id || key !== $('studio-key').value || token !== generation) throw new Error('Produto ou credencial alterado; atualize o plano.');
    const data = await response.json();
    if (!response.ok && data.detail?.code === 'delivery_preflight_blocked') {
      showPreflight(data.detail.preflight);
      throw new Error('Execução não iniciada. Resolva os bloqueios da checagem abaixo.');
    }
    if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Confira os campos e limites do plano (' + response.status + ').');
    return data;
  }
  function availability() {
    const current = plan?.features.find(f=>f.status !== 'merged');
    $('delivery-start').disabled = busy || preflight?.can_start === false || !enabled || !current || !['pending','failed','cancelled'].includes(current.status) || current.attempts.length >= 3;
    $('delivery-preflight-button').disabled = busy || !plan;
    $('delivery-start').textContent = current?.attempts.length ? 'Autorizar nova tentativa' : 'Executar próxima entrega';
  }
  async function action(fn) {
    if (busy) return;
    busy = true; availability();
    try { await fn(); } catch (error) {message(error.message);}
    finally {busy = false; availability();}
  }
  function render(data) {
    clearPreflight();
    plan = data.plan; enabled = data.factory_enabled;
    $('delivery-plan-form').hidden = !!plan;
    $('delivery-ledger').hidden = !plan;
    $('delivery-approved').checked = false;
    if (!plan) {message('Revise as entregas e escolha o repositório. Salvar o plano não inicia execução.'); availability(); return;}
    $('delivery-target').textContent = plan.repository + ' · ' + plan.base_ref + ' · revisão ' + plan.revision;
    $('delivery-rules').textContent = JSON.stringify({decisions:plan.decisions, preservation_constraints:plan.preservation_constraints},null,2);
    const list = $('delivery-features-list'); list.replaceChildren();
    plan.features.forEach(feature => {
      const row = document.createElement('li'), title = document.createElement('h3'), status = document.createElement('p');
      if (feature.status === 'merged') row.className = 'delivery-merged';
      title.textContent = feature.title; status.textContent = names[feature.status] || feature.status;
      row.append(title, status);
      const criteria = document.createElement('ul');
      feature.acceptance_criteria.forEach(text=>{const li=document.createElement('li');li.textContent=text;criteria.append(li);});
      row.append(criteria);
      feature.attempts.forEach(attempt => {
        const meta = document.createElement('small');
        meta.textContent = 'Workflow ' + attempt.workflow_id + ' · ' + (names[attempt.status] || attempt.status);
        row.append(meta);
        const context = document.createElement('button'); context.type='button'; context.textContent='Ver contexto';
        context.onclick=()=>action(async()=>{
          const value=await api('/context/'+encodeURIComponent(attempt.workflow_id));
          $('delivery-context').textContent=JSON.stringify(value,null,2);
          $('delivery-context').parentElement.open=true;
        }); row.append(context);
        const number = attempt.receipt?.pull_request_number || attempt.pull_request_number;
        if (Number.isInteger(number) && number > 0 && /^[a-z0-9_-]+\/[a-z0-9_.-]+$/i.test(plan.repository)) {
          const link=document.createElement('a'); link.textContent='Abrir PR #'+number;
          link.href='https://github.com/'+plan.repository+'/pull/'+number; link.target='_blank'; link.rel='noopener noreferrer'; row.append(link);
        }
      });
      list.append(row);
    });
    const current=plan.features.find(f=>f.status!=='merged');
    message(!enabled ? 'Plano salvo. Factory mode está desativado: nenhuma execução pode ser iniciada.'
      : !current ? 'Todas as entregas têm merge verificado. Acrescente a próxima evolução.'
      : current.status==='dispatch_unknown' || current.status==='dispatching' ? 'Envio ainda não confirmado. Verifique o workflow; não reenviamos automaticamente para evitar duplicação.'
      : 'Próxima entrega: '+current.title+'. Verifique execução e merge antes de avançar.');
    availability();
  }
  window.addEventListener('forgehand:product', event=>{
    clearPreflight(); $('delivery-approved').checked=false;
    product=event.detail; plan=null; const token=++generation;
    $('delivery-area').hidden=product.status!=='ready_for_preview';
    $('delivery-context').textContent='Selecione “Ver contexto” em uma entrega executada.';
    if (product.status!=='ready_for_preview') return;
    $('delivery-plan-form').hidden=true; $('delivery-ledger').hidden=true;
    $('delivery-features').value=JSON.stringify(product.brief.backlog.map(title=>({
      title:title.slice(0,120), description:'Implementar e verificar: '+title,
      acceptance_criteria:['Definir um critério específico e verificável para: '+title]
    })),null,2);
    api().then(data=>{if(token===generation)render(data);}).catch(error=>{if(token===generation)message(error.message);});
  });
  $('delivery-refresh').onclick=()=>action(async()=>render(await api()));
  $('delivery-preflight-button').onclick=()=>action(async()=>showPreflight(await api('/preflight')));
  $('studio-key').addEventListener('input',()=>{generation++;clearPreflight();$('delivery-approved').checked=false;availability();});
  window.addEventListener('forgehand:idle', availability);
  $('delivery-plan-form').onsubmit=event=>{event.preventDefault();action(async()=>render(await api('',{
    repository:$('delivery-repository').value.trim(), base_ref:$('delivery-base').value.trim(),
    build_profile:$('delivery-profile').value.trim() || null,
    preservation_constraints:lines('delivery-preserve'), decisions:lines('delivery-decisions'),
    features:reviewedFeatures('delivery-features')
  },'PUT')));};
  $('delivery-start-form').onsubmit=event=>{event.preventDefault();action(async()=>{
    if(!plan || !enabled || preflight?.can_start === false || !$('delivery-approved').checked) throw new Error('Confirme a autorização e resolva os bloqueios da fábrica.');
    render(await api('/start',{revision:plan.revision,approved:true,max_cost_usd:Number($('delivery-budget').value)},'POST'));
  });};
  $('delivery-reconcile').onclick=()=>action(async()=>render(await api('/reconcile',{},'POST')));
  $('delivery-append-form').onsubmit=event=>{event.preventDefault();action(async()=>{
    render(await api('/append',{revision:plan.revision,features:reviewedFeatures('delivery-add-features'),decisions:lines('delivery-add-decisions')},'POST'));
    $('delivery-add-features').value='[]'; $('delivery-add-decisions').value='';
  });};
})();
