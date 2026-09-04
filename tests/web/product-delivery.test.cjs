const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function fixture({enabled=true, status='pending', blockOnStart=false}={}) {
  class Element {
    constructor(tag='div') {this.tag=tag;this.children=[];this.value='';this.checked=false;this.textContent='';this.disabled=false;this.listeners={};}
    addEventListener(name, fn) {this.listeners[name]=fn;}
    append(...children) {children.forEach(child=>{child.parentElement=this;this.children.push(child);});}
    replaceChildren() {this.children=[];}
    all() {return [this,...this.children.flatMap(child=>child.all())];}
  }
  const elements=new Map(), get=id=>{if(!elements.has(id))elements.set(id,new Element());return elements.get(id);};
  get('studio-key').value='test-key'; get('delivery-budget').value='0.2';
  const listeners={}, calls=[];
  const report={product_id:'product',revision:3,can_start:true,checks:[{code:'workers',status:'pass',message:'Workers disponíveis'}],not_checked:['Docker não verificado']};
  let plan={product_id:'product',repository:'acme/agenda',base_ref:'main',revision:3,
    decisions:['Usar transações'],preservation_constraints:['Preservar registros'],
    features:[{id:'feature',title:'<img src=x onerror=alert(1)>',status,
      acceptance_criteria:['Preservar dados'],attempts:[]}]};
  const context=vm.createContext({document:{getElementById:get,createElement:tag=>new Element(tag)},
    window:{addEventListener:(name,fn)=>{listeners[name]=fn;}},
    fetch:async(url,options)=>{
      calls.push({url,...options});
      if(url.endsWith('/preflight'))return {ok:true,json:async()=>report};
      if(url.endsWith('/start') && blockOnStart)return {ok:false,status:409,json:async()=>({detail:{code:'delivery_preflight_blocked',preflight:{...report,can_start:false,checks:[{code:'workers',status:'block',message:'Worker indisponível'}]}}})};
      if(url.endsWith('/start'))plan={...plan,revision:4,features:[{...plan.features[0],status:'running',attempts:[{workflow_id:'workflow',status:'running'}]}]};
      return {ok:true,json:async()=>({plan,factory_enabled:enabled})};
    }});
  vm.runInContext(fs.readFileSync(path.join(__dirname,'../../app/web/product-delivery.js'),'utf8'),context);
  const load=()=>listeners['forgehand:product']({detail:{id:'product',status:'ready_for_preview',brief:{backlog:['Disponibilidade']}}});
  const tick=()=>new Promise(resolve=>setImmediate(resolve));
  return {get,calls,load,tick,listeners,report};
}

test('delivery ledger loads without dispatch and treats titles as text',async()=>{
  const f=fixture(); f.load(); await f.tick();
  assert.equal(f.calls.length,1); assert.equal(f.calls[0].method,'GET');
  const title=f.get('delivery-features-list').all().find(el=>el.tag==='h3');
  assert.equal(title.textContent,'<img src=x onerror=alert(1)>');
  assert.equal(f.get('delivery-features-list').all().some(el=>el.tag==='img'),false);
  assert.equal(f.get('delivery-approved').checked,false);
  assert.deepEqual(JSON.parse(f.get('delivery-rules').textContent),{decisions:['Usar transações'],preservation_constraints:['Preservar registros']});
});

test('provisional acceptance criteria cannot be saved from the Studio',async()=>{
  const f=fixture(); f.load(); await f.tick();
  f.get('delivery-plan-form').onsubmit({preventDefault(){}}); await f.tick();
  assert.equal(f.calls.length,1);
  assert.match(f.get('delivery-message').textContent,/Substitua os critérios provisórios/);
});

test('execution needs explicit approval and sends revision and per-attempt budget once',async()=>{
  const f=fixture(); f.load(); await f.tick();
  const event={preventDefault(){}};
  f.get('delivery-start-form').onsubmit(event); await f.tick();
  assert.equal(f.calls.length,1);
  f.get('delivery-approved').checked=true;
  f.get('delivery-start-form').onsubmit(event);
  f.get('delivery-start-form').onsubmit(event);
  await f.tick();
  assert.equal(f.calls.length,2);
  assert.deepEqual(JSON.parse(f.calls[1].body),{revision:3,approved:true,max_cost_usd:.2});
  assert.equal(f.get('delivery-start').disabled,true);
  assert.equal(f.get('delivery-approved').checked,false);
});

test('factory-disabled gate survives the legacy studio idle event',async()=>{
  const f=fixture({enabled:false}); f.load(); await f.tick();
  f.get('delivery-start').disabled=false; f.listeners['forgehand:idle']();
  assert.equal(f.get('delivery-start').disabled,true);
  f.get('delivery-approved').checked=true;
  f.get('delivery-start-form').onsubmit({preventDefault(){}}); await f.tick();
  assert.equal(f.calls.length,1);
});

test('unmerged and uncertain attempts never present an enabled next-delivery button',async()=>{
  for(const status of ['dispatching','dispatch_unknown','running','awaiting_review','awaiting_decision','blocked','merged']) {
    const f=fixture({status}); f.load(); await f.tick();
    assert.equal(f.get('delivery-start').disabled,true,status);
  }
});

test('preflight displays safe text, never starts a workflow and clears approval',async()=>{
  const f=fixture(); f.load(); await f.tick();
  f.report.checks[0].message='<img src=x onerror=alert(1)>';
  f.get('delivery-approved').checked=true;
  f.get('delivery-preflight-button').onclick(); await f.tick();
  assert.equal(f.calls.length,2);
  assert.equal(f.calls[1].method,'GET');
  assert.match(f.calls[1].url,/\/preflight$/);
  assert.equal(f.get('delivery-approved').checked,false);
  const rows=f.get('delivery-preflight-checks').all();
  assert.equal(rows.some(el=>el.tag==='img'),false);
  assert.equal(rows.some(el=>el.textContent.includes('<img')),true);
  assert.match(f.get('delivery-preflight-limits').textContent,/Docker não verificado/);
});

test('blocked preflight disables execution but permits rechecking',async()=>{
  const f=fixture(); f.load(); await f.tick();
  f.report.can_start=false;
  f.get('delivery-preflight-button').onclick(); await f.tick();
  assert.equal(f.get('delivery-start').disabled,true);
  assert.equal(f.get('delivery-preflight-button').disabled,false);
  f.get('delivery-approved').checked=true;
  f.get('delivery-start-form').onsubmit({preventDefault(){}}); await f.tick();
  assert.equal(f.calls.length,2);
  f.report.can_start=true;
  f.get('delivery-preflight-button').onclick(); await f.tick();
  assert.equal(f.get('delivery-start').disabled,false);
  assert.equal(f.get('delivery-approved').checked,false);
});

test('reports are invalidated by credential edits and plan reloads',async()=>{
  const f=fixture(); f.load(); await f.tick();
  f.get('delivery-preflight-button').onclick(); await f.tick();
  f.get('studio-key').value='different-key';
  f.get('studio-key').listeners.input();
  assert.equal(f.get('delivery-preflight-checks').children.length,0);
  assert.equal(f.get('delivery-approved').checked,false);
  f.get('delivery-preflight-button').onclick(); await f.tick();
  f.load(); await f.tick();
  assert.equal(f.get('delivery-preflight-checks').children.length,0);
  f.report.revision=2;
  f.get('delivery-preflight-button').onclick(); await f.tick();
  assert.equal(f.get('delivery-preflight-checks').children.length,0);
  assert.match(f.get('delivery-message').textContent,/Plano alterado/);
});

test('server start blockers render the fresh report instead of a generic error',async()=>{
  const f=fixture({blockOnStart:true}); f.load(); await f.tick();
  f.get('delivery-preflight-button').onclick(); await f.tick();
  f.get('delivery-approved').checked=true;
  f.get('delivery-start-form').onsubmit({preventDefault(){}}); await f.tick();
  assert.equal(f.get('delivery-start').disabled,true);
  assert.equal(f.get('delivery-approved').checked,false);
  assert.match(f.get('delivery-message').textContent,/Execução não iniciada/);
  assert.equal(f.get('delivery-preflight-checks').all().some(el=>el.textContent.includes('Worker indisponível')),true);
});
