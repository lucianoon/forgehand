const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

function demo() {
  class Element {
    constructor(tag) {this.tag = tag; this.children = []; this.dataset = {}; this.value = ''; this.textContent = '';}
    append(child) {this.children.push(child);}
    replaceChildren() {this.children = [];}
    setAttribute(key, value) {this[key] = value;}
    reportValidity() {return !this.all().some(el => el.required && !el.value.trim());}
    all() {return [this, ...this.children.flatMap(child => child.all())];}
    querySelector() {return this.all().find(el => el.tag === 'input');}
    focus() {}
    click() {this.onclick?.();} // Sandboxed native form submission is unavailable.
  }
  const root = new Element('main');
  const model = {name: 'Agenda', description: 'Demo', theme: 'forest', entities: [
    {id:'appointment', name:'Agendamento', fields:[{id:'client', label:'Cliente', kind:'text', required:true, options:[]}], records:[]},
  ]};
  const context = vm.createContext({document:{body: new Element('body'),
    getElementById: id => id === 'demo' ? root : {textContent:JSON.stringify(model)},
    createElement: tag => new Element(tag)}, window:{}, URL, Blob, setTimeout});
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../../app/web/demo.js'), 'utf8'), context);
  const button = text => root.all().find(el => el.tag === 'button' && el.textContent === text);
  const client = () => root.all().find(el => el.name === 'client');
  return {root, button, client};
}

test('sandbox-safe save validates, creates, edits, searches and deletes records', () => {
  const {root, button, client} = demo();
  button('Adicionar registro').click();
  assert.ok(button('Agendamento (0)'));
  client().value = 'Cliente QA';
  button('Adicionar registro').click();
  assert.ok(button('Agendamento (1)'));
  button('Editar').click();
  client().value = 'Cliente editado';
  button('Salvar alterações').click();
  assert.ok(root.all().some(el => el.tag === 'dd' && el.textContent === 'Cliente editado'));
  let search = root.all().find(el => el.type === 'search');
  search.value = 'inexistente'; search.oninput();
  assert.equal(root.all().filter(el => el.tag === 'article').length, 0);
  search.value = 'editado'; search.oninput();
  assert.equal(root.all().filter(el => el.tag === 'article').length, 1);
  button('Excluir').click();
  assert.ok(button('Agendamento (1)'));
  button('Confirmar exclusão').click();
  assert.ok(button('Agendamento (0)'));
});
