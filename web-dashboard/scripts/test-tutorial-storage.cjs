/* Exercise actual record writers against the practice storage boundary. */
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const root=path.resolve(__dirname,'../dist'),persisted=new Map(),writes=[];
const box={console,Map,Intl,Date,localStorage:{getItem:k=>persisted.get(k)??null,setItem:(k,v)=>{writes.push(k);persisted.set(k,String(v));}},state:{store:'S01',date:'2026-09-30',overrides:{}},render(){}};
vm.createContext(box);
for(const name of ['workspace-storage','daily-ledger','promotion-ui'])vm.runInContext(fs.readFileSync(path.join(root,name+'.js'),'utf8'),box);
vm.runInContext("DailyLedger.save({S01:{original:{date:'2026-10-01'}}});savePromotionPlans([{name:'기존 행사'}]);WorkspaceStorage.setItem('fresh-order-settings-v2','original settings')",box);
const original=JSON.stringify([...persisted]),count=writes.length,epoch=vm.runInContext('WorkspaceStorage.generation',box);
vm.runInContext("WorkspaceStorage.beginPractice();DailyLedger.save({S01:{practice:{date:'2026-10-02'}}});savePromotionPlans([{name:'연습 행사'}]);WorkspaceStorage.setItem('fresh-order-settings-v2','practice settings')",box);
assert.equal(JSON.stringify([...persisted]),original,'practice must not write real records, promotions or settings');
assert.equal(writes.length,count);
assert.equal(vm.runInContext("DailyLedger.load().S01.practice.date",box),'2026-10-02');
assert.equal(vm.runInContext("WorkspaceStorage.getItem('fresh-order-settings-v2')",box),'practice settings');
vm.runInContext('WorkspaceStorage.endPractice()',box);
assert.equal(vm.runInContext('WorkspaceStorage.generation',box),epoch+2,'in-flight extraction can detect leaving practice');
assert.equal(vm.runInContext('DailyLedger.load().S01.original.date',box),'2026-10-01');
assert.equal(vm.runInContext("WorkspaceStorage.getItem('fresh-order-settings-v2')",box),'original settings');
vm.runInContext('loadPromotions()',box);
assert.equal(vm.runInContext("getPromotionPlans()[0].name",box),'기존 행사');
// A refreshed page has no practice layer and reads exactly the original durable data.
vm.runInContext(fs.readFileSync(path.join(root,'workspace-storage.js'),'utf8'),box);
assert.equal(vm.runInContext('WorkspaceStorage.practicing',box),false);
assert.equal(JSON.stringify([...persisted]),original);
// The tour must remain usable when browser storage is denied.
box.localStorage.setItem=()=>{throw Error('storage denied');};box.localStorage.getItem=()=>{throw Error('storage denied');};
vm.runInContext("WorkspaceStorage.beginPractice();DailyLedger.save({});WorkspaceStorage.endPractice()",box);
console.log('PASS: real ledger, promotion and preference writes isolated; original values survive exit/reload; generation guards async requests; denied storage supports practice');
