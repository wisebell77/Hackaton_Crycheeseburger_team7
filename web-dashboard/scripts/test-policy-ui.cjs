const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const root=require('node:path').resolve(__dirname,'../dist'),read=f=>JSON.parse(fs.readFileSync(`${root}/${f}.json`));
for(const f of ['model-engine','promotion-engine','engine-core','policy-engine'])vm.runInThisContext(fs.readFileSync(`${root}/${f}.js`,'utf8'));
const data=read('data'),promo=read('promotion-inputs'),team=read('team-forecasts'),params=read('engine-params');OrderEngine.configure(params);
const date='2026-09-30',store='S01',win=team.windows[date],raw=data.snapshots[store+'_'+date],proj=PromotionEngine.project(promo,date,store,win.dates,[]),before=JSON.stringify(raw),ctx={asOf:date,storeId:store,dates:win.dates,suppliers:data.suppliers};
const p=OrderPolicy.defaults(),base=OrderPolicy.evaluate(raw,ctx,proj,promo,p);assert(base.best);assert(base.best.feasible);assert(base.ranked.every(c=>c.score>=base.best.score));assert.equal(JSON.stringify(raw),before);
const patch={status:'ready',audit:[{field:'mode',after:'safe'},{field:'budget',after:{won:200000,operator:'lt'}},{field:'excluded_ingredient_ids',after:['I016']}]};
p.constraints.excluded_ingredient_ids=['I001'];const merged=OrderPolicy.merge(p,patch);assert.equal(p.preferences.service_level,.95);assert.equal(p.constraints.budget,null);assert.equal(merged.preferences.service_level,.99);assert.deepEqual(merged.constraints.excluded_ingredient_ids,['I001','I016']);
const constrained=OrderPolicy.evaluate(raw,ctx,proj,promo,merged);assert(constrained.ranked.every(c=>c.purchase<200000));assert(constrained.candidates.every(c=>!c.quantities.I016&&!c.quantities.I001));
const custom=OrderPolicy.evaluate(raw,{...ctx,quantities:{...base.best.quantities,I001:1.5}},proj,promo,p);assert.equal(custom.best,null);
const impossible=OrderPolicy.defaults();impossible.constraints.budget={won:0,operator:'lt'};assert.equal(OrderPolicy.evaluate(raw,ctx,proj,promo,impossible).best,null);
const buffer=OrderPolicy.defaults();buffer.preferences.extra_demand_buffer_ratio=.2;const r=OrderPolicy.evaluate(raw,ctx,proj,promo,buffer);for(const i of raw)assert(r.engine.presets.current.recommend[i.ingredient_id].order_qty>=base.engine.presets.current.recommend[i.ingredient_id].order_qty);
const delayed=OrderPolicy.defaults();delayed.scenario.delay_buffer_days=2;const delay=OrderPolicy.evaluate(raw,ctx,proj,promo,delayed);for(const i of delay.items){const original=raw.find(x=>x.ingredient_id===i.ingredient_id);assert.equal(i.sc.arrival,base.items.find(x=>x.ingredient_id===i.ingredient_id).sc.arrival);if(original.pending.length)assert.notEqual(i.pending[0].date,original.pending[0].date);}
const item=raw[0];assert.throws(()=>OrderPolicy.merge(p,{status:'needs_clarification'}));
console.log('PASS: shared-score ranking, persistent/order precedence, exclusions, strict budget, infeasibility, manual constraints, buffer, existing-PO delay, immutability');
