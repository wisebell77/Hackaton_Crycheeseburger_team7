const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict'),path=require('node:path');
const root=path.resolve(__dirname,'..');vm.runInThisContext(fs.readFileSync(root+'/dist/promotion-engine.js','utf8'));
const data=JSON.parse(fs.readFileSync(root+'/dist/promotion-inputs.json')),team=JSON.parse(fs.readFileSync(root+'/dist/team-forecasts.json')),fixture=JSON.parse(fs.readFileSync(__dirname+'/promotion-fixture.json'));
let checks=0;
for(const [asof,w]of Object.entries(team.windows))for(const store of ['S01','S02','S03']){
 const actual=PromotionEngine.project(data,asof,store,w.dates,[]).ingredients;
 for(const [iid,qty]of Object.entries(w.stores[store]))for(let d=0;d<7;d++){assert(Math.abs(actual[iid][d]-qty[d])<1e-7);checks++;}
}
const dates=team.windows['2026-09-30'].dates,plan=fixture.plan.promotions;
for(const store of ['S01','S02','S03']){
 const actual=PromotionEngine.project(data,'2026-09-30',store,dates,store==='S02'?plan:[]).ingredients;
 for(const [iid,qty] of Object.entries(fixture.expected[store]))for(let d=0;d<7;d++)assert(Math.abs(actual[iid][d]-qty[d])<1e-7);
}
assert.throws(()=>PromotionEngine.project(data,'2026-09-30','S02',dates,[...plan,{...plan[0],promotion_id:'OTHER'}]),/중복/);
let p=structuredClone(plan);p[0].response.uplift_pct=NaN;assert.throws(()=>PromotionEngine.project(data,'2026-09-30','S02',dates,p));
p=structuredClone(plan);p[0].offer.value=20;assert.deepEqual(PromotionEngine.project(data,'2026-09-30','S02',dates,p),PromotionEngine.project(data,'2026-09-30','S02',dates,plan));
p=structuredClone(plan);p[0].kind='special';p[0].offer={type:'none',value:0};assert.deepEqual(PromotionEngine.project(data,'2026-09-30','S02',dates,p),PromotionEngine.project(data,'2026-09-30','S02',dates,plan));
p=structuredClone(plan);p[0].response={mode:'historical_reference',reference_promo_id:'P01',accept_transfer_assumption:false};assert.throws(()=>PromotionEngine.project(data,'2026-09-30','S02',dates,p),/확인/);
console.log(`PASS: ${checks} baseline values + 462 Python promotion outputs; overlap, empty/invalid response, explicit reference confirmation, discount independence, general demand scenario`);
