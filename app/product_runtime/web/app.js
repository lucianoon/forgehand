(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  let model, active = 0, editing = null, offset = 0, busy = false;
  const pageSize = 25;
  function el(tag, text, parent) {
    const node = document.createElement(tag); if (text !== undefined) node.textContent = text;
    parent.append(node); return node;
  }
  function message(text, error = false) {$('message').textContent = text; $('message').dataset.error = String(error);}
  function signedOut() {
    model = null; editing = null;
    $('login').hidden = false; $('workspace').hidden = true; $('logout').hidden = true;
    $('identity').textContent = ''; $('records').replaceChildren(); $('record-form').replaceChildren();
    $('product-name').textContent = 'Entre para continuar';
    $('product-description').textContent = 'Seus registros ficam salvos no servidor e separados por usuário.';
  }
  async function api(path, options = {}) {
    const response = await fetch('/api' + path, {...options, credentials:'same-origin', headers:{'Content-Type':'application/json'}});
    if (!response.ok) {
      const body = await response.json().catch(()=>({}));
      if (response.status === 401) signedOut();
      const error = new Error(typeof body.detail === 'string' ? body.detail : 'Revise os campos informados.');
      error.status = response.status; throw error;
    }
    if (response.status === 204) return null;
    return response.json();
  }
  async function action(callback) {
    if (busy) return;
    busy = true; document.querySelectorAll('button').forEach(button=>{button.disabled = true;});
    try {await callback();} catch(error) {message(error.message, true);}
    finally {
      busy = false; document.querySelectorAll('button').forEach(button=>{button.disabled = button.dataset.unavailable === 'true';});
    }
  }
  function entity() {return model.entities[active];}
  function renderForm() {
    const form = $('record-form'); form.replaceChildren();
    el('h3', editing ? 'Editar registro' : 'Novo registro', form);
    entity().fields.forEach(field => {
      const label = el('label', field.label, form);
      const input = el(field.kind === 'select' ? 'select' : 'input', undefined, label);
      input.name = field.id; input.required = field.required;
      if (field.kind === 'select') {
        el('option','Selecione…',input).value = '';
        field.options.forEach(option=>{el('option',option,input).value = option;});
      } else {input.type = field.kind; input.maxLength = 300; if(field.kind === 'number') input.step = 'any';}
      if(editing) input.value = editing.values[field.id] || '';
    });
    const actions = el('div',undefined,form); actions.className = 'actions';
    const save = el('button',editing ? 'Salvar alterações' : 'Salvar registro',actions); save.type = 'submit'; save.className = 'primary';
    if(editing) {
      const cancel = el('button','Cancelar edição',actions); cancel.type='button';
      cancel.onclick = ()=>{editing=null; renderForm();};
    }
  }
  async function loadRows() {
    const result = await api('/records/' + entity().id + '?limit=' + pageSize + '&offset=' + offset + '&q=' + encodeURIComponent($('search').value));
    const list = $('records'); list.replaceChildren();
    if(!result.items.length) el('p','Nenhum registro encontrado. Cadastre um ou revise a busca.',list);
    result.items.forEach(row=>{
      const card = el('article',undefined,list), details = el('dl',undefined,card);
      entity().fields.forEach(field=>{el('dt',field.label,details); el('dd',row.values[field.id] || '—',details);});
      const actions = el('div',undefined,card); actions.className='actions';
      const edit=el('button','Editar',actions); edit.onclick=()=>{editing=row; renderForm(); $('record-form').querySelector('input,select')?.focus();};
      const remove=el('button','Excluir',actions);
      remove.onclick=()=>{
        if(!remove.dataset.confirmed) {remove.dataset.confirmed='true'; remove.textContent='Confirmar exclusão'; return;}
        action(async()=>{await api('/records/'+entity().id+'/'+row.id+'?version='+row.version,{method:'DELETE'}); editing=null; renderForm(); await loadRows(); message('Registro excluído.');});
      };
    });
    $('page').textContent='Página '+(offset/pageSize+1);
    $('previous').dataset.unavailable=String(offset===0); $('previous').disabled=offset===0;
    $('next').dataset.unavailable=String(!result.has_more); $('next').disabled=!result.has_more;
  }
  async function selectEntity(index) {
    active=index; editing=null; offset=0; $('search').value='';
    $('entity-name').textContent=entity().name; $('entities').replaceChildren();
    model.entities.forEach((item,i)=>{
      const button=el('button',item.name,$('entities')); button.setAttribute('aria-pressed',String(i===active));
      button.onclick=()=>action(()=>selectEntity(i));
    });
    renderForm(); await loadRows();
  }
  async function loadSession() {
    const user=await api('/me'); model=await api('/model');
    document.body.dataset.theme=model.theme; document.title=model.name;
    $('product-name').textContent=model.name; $('product-description').textContent=model.description;
    $('identity').textContent='Conectado como '+user.username+' · dados privados da sua conta';
    $('login').hidden=true; $('workspace').hidden=false; $('logout').hidden=false;
    await selectEntity(0); message('Dados carregados do servidor.');
  }
  $('login').onsubmit=event=>{event.preventDefault(); action(async()=>{
    const form=new FormData($('login'));
    await api('/login',{method:'POST',body:JSON.stringify({username:form.get('username'),password:form.get('password')})});
    $('login').reset(); await loadSession();
  });};
  $('logout').onclick=()=>action(async()=>{await api('/logout',{method:'POST',body:'{}'}); signedOut(); message('Sessão encerrada.');});
  $('record-form').onsubmit=event=>{event.preventDefault(); action(async()=>{
    const values=Object.fromEntries(new FormData($('record-form')));
    const path='/records/'+entity().id+(editing?'/'+editing.id:'');
    await api(path,{method:editing?'PUT':'POST',body:JSON.stringify({values,...(editing?{version:editing.version}:{})})});
    editing=null; renderForm(); await loadRows(); message('Registro salvo no servidor.');
  });};
  $('refresh').onclick=()=>action(async()=>{editing=null; renderForm(); offset=0; await loadRows(); message('Lista atualizada.');});
  $('search').onsearch=()=>action(async()=>{offset=0; await loadRows();});
  $('search').onkeydown=event=>{if(event.key==='Enter'){event.preventDefault(); action(async()=>{offset=0; await loadRows();});}};
  $('previous').onclick=()=>action(async()=>{offset=Math.max(0,offset-pageSize); await loadRows();});
  $('next').onclick=()=>action(async()=>{offset+=pageSize; await loadRows();});
  $('export').onclick=()=>action(async()=>{
    const data=await api('/export');
    const url=URL.createObjectURL(new Blob([JSON.stringify(data,null,2)],{type:'application/json'}));
    const link=document.createElement('a'); link.href=url; link.download='dados.json'; link.click();
    setTimeout(()=>URL.revokeObjectURL(url),1000); message('Exportação preparada.');
  });
  action(async()=>{
    try {await loadSession();} catch(error) {
      if(error.status === 401) message('Use sua conta para acessar os registros.');
      else throw error;
    }
  });
})();
