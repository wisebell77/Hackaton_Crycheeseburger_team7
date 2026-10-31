const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const root=require('node:path').resolve(__dirname,'..');
const team=JSON.parse(fs.readFileSync(root+'/dist/team-forecasts.json'));
const data=JSON.parse(fs.readFileSync(root+'/dist/data.json'));
vm.runInThisContext(fs.readFileSync(root+'/dist/model-engine.js','utf8'));
FreshOrderEngine.configure({forecast:({item,storeId,asOf})=>{const v=team.windows[asOf]?.stores[storeId]?.[item.ingredient_id];if(!v)throw Error('Missing forecast');return v;}});
let count=0;
for(const [asof,window] of Object.entries(team.windows)){
 assert.equal(window.dates.length,7);assert(window.data_through<=asof);
 for(const store of data.stores){
 const snapshot=data.snapshots[store.store_id+'_2026-09-30'];
 const before=JSON.stringify(snapshot);
 const items=FreshOrderEngine.buildItems(snapshot,{storeId:store.store_id,asOf:asof,suppliers:data.suppliers,overrides:{}});
 assert.equal(items.length,22);
 for(const i of items){assert.equal(i.forecast.length,7);assert(i.forecast.every(x=>Number.isFinite(x)&&x>=0));assert.equal(i.total,i.forecast.reduce((a,b)=>a+b,0));assert(Number.isInteger(i.recommended)&&i.recommended>=0);count++;}
 assert.equal(JSON.stringify(snapshot),before);
 }
}
const v=team.windows['2026-09-30'].stores.S01.I001;assert.equal(v[0],707.5);assert(Math.abs(v.reduce((a,b)=>a+b,0)-3793.54652584741)<1e-8);
assert.throws(()=>FreshOrderEngine.buildItems(data.snapshots.S01_2026_09_30||data.snapshots['S01_2026-09-30'],{storeId:'S01',asOf:'2026-10-15',suppliers:data.suppliers,overrides:{}}),/Missing forecast/);
console.log(`PASS: ${count} ingredient windows, model reference, missing-date guard, stock immutability`);
