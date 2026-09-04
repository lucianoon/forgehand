/* Trusted renderer: model content only reaches textContent or form values. */
(() => {
  'use strict';
  const model = JSON.parse(document.getElementById('model').textContent);
  const root = document.getElementById('demo');
  document.body.dataset.theme = model.theme;
  const data = model.entities.map(entity => entity.records.map(row => [...row.values]));
  let active = 0, editing = null, query = '';
  function el(tag, text, parent = root) {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = text;
    parent.append(node); return node;
  }
  const header = el('header');
  el('small', 'PRIMEIRA VERSÃO · DADOS DE DEMONSTRAÇÃO', header);
  el('h1', model.name, header);
  el('p', model.description, header);
  const nav = el('nav'); nav.setAttribute('aria-label', 'Cadastros');
  const content = el('section');
  const notice = el('p', 'Dados apenas nesta sessão. Exporte antes de fechar. Sem login, servidor ou banco compartilhado.');
  notice.className = 'notice';
  function render() {
    nav.replaceChildren(); content.replaceChildren();
    model.entities.forEach((entity, index) => {
      const button = el('button', entity.name + ' (' + data[index].length + ')', nav);
      button.type = 'button'; button.setAttribute('aria-pressed', String(index === active));
      button.onclick = () => {active = index; editing = null; query = ''; render();};
    });
    const entity = model.entities[active];
    const toolbar = el('div', undefined, content); toolbar.className = 'toolbar';
    el('h2', entity.name, toolbar);
    const exportButton = el('button', 'Exportar dados', toolbar);
    exportButton.onclick = () => {
      const output = Object.fromEntries(model.entities.map((item, i) =>
        [item.id, data[i].map(row => Object.fromEntries(item.fields.map((field, j) => [field.id, row[j]])))]));
      const url = URL.createObjectURL(new Blob([JSON.stringify(output, null, 2)], {type: 'application/json'}));
      const link = document.createElement('a'); link.href = url; link.download = 'dados.json';
      link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
    };
    const form = el('form', undefined, content);
    el('h3', editing === null ? 'Novo registro' : 'Editar registro', form);
    const fields = el('div', undefined, form); fields.className = 'fields';
    const inputs = entity.fields.map((field, index) => {
      const label = el('label', field.label, fields);
      const input = el(field.kind === 'select' ? 'select' : 'input', undefined, label);
      input.name = field.id; input.required = field.required;
      if (field.kind === 'select') {
        el('option', 'Selecione…', input).value = '';
        field.options.forEach(option => {el('option', option, input).value = option;});
      } else {
        input.type = field.kind; input.maxLength = 300;
        if (field.kind === 'number') input.step = 'any';
      }
      if (editing !== null) input.value = data[active][editing][index];
      return input;
    });
    const actions = el('div', undefined, form); actions.className = 'actions';
    const save = el('button', editing === null ? 'Adicionar registro' : 'Salvar alterações', actions);
    // Native form submission is disabled by the preview sandbox. Save locally
    // through a button handler while retaining HTML field validation.
    save.type = 'button'; save.className = 'primary';
    if (editing !== null) {
      const cancel = el('button', 'Cancelar edição', actions); cancel.type = 'button';
      cancel.onclick = () => {editing = null; render();};
    }
    function saveRecord() {
      if (!form.reportValidity()) return;
      const values = inputs.map(input => input.value.trim());
      if (editing === null) data[active].push(values); else data[active][editing] = values;
      editing = null; render();
    }
    save.onclick = saveRecord;
    form.onsubmit = event => {event.preventDefault(); saveRecord();};
    form.onkeydown = event => {
      if (event.key === 'Enter' && event.target.tagName === 'INPUT') {
        event.preventDefault(); saveRecord();
      }
    };
    const label = el('label', 'Buscar registros', content);
    const search = el('input', undefined, label); search.type = 'search'; search.value = query;
    const list = el('div', undefined, content); list.className = 'records';
    function renderRows() {
      list.replaceChildren();
      const rows = data[active].map((row, index) => ({row, index}))
        .filter(({row}) => row.join(' ').toLocaleLowerCase().includes(query.toLocaleLowerCase()));
      if (!rows.length) {el('p', 'Nenhum registro encontrado. Adicione um pelo formulário acima.', list); return;}
      rows.forEach(({row, index}) => {
        const card = el('article', undefined, list);
        const details = el('dl', undefined, card);
        entity.fields.forEach((field, j) => {el('dt', field.label, details); el('dd', row[j] || '—', details);});
        const buttons = el('div', undefined, card); buttons.className = 'actions';
        const edit = el('button', 'Editar', buttons); edit.type = 'button';
        edit.onclick = () => {editing = index; render(); content.querySelector('input,select')?.focus();};
        const remove = el('button', 'Excluir', buttons); remove.type = 'button';
        remove.onclick = () => {
          if (remove.dataset.confirmed) {data[active].splice(index, 1); editing = null; render();}
          else {remove.dataset.confirmed = 'yes'; remove.textContent = 'Confirmar exclusão';}
        };
      });
    }
    search.oninput = () => {query = search.value; renderRows();};
    renderRows();
  }
  render();
})();
