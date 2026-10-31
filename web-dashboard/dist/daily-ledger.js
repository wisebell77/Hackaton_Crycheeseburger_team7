/* Append-only daily close records. Historical seed data is never mutated. */
(function(){
const SEED='2026-09-30',KEY='fresh-order-daily-ledger-v1';
const add=(s,n)=>{const d=new Date(s+'T00:00:00Z');d.setUTCDate(d.getUTCDate()+n);return d.toISOString().slice(0,10)};
function nextDate(records,store){const dates=Object.keys(records[store]||{}).sort();let previous=SEED;for(const ds of dates){if(ds!==add(previous,1))break;previous=ds}return add(previous,1)}
function previousSnapshot(data,records,store,ds){const prev=add(ds,-1);return prev===SEED?data.snapshots[store+'_'+SEED]:records[store]?.[prev]?.items||null;}
function projectForecast(item,asOf){const offset=Math.round((new Date(asOf+'T00:00:00Z')-new Date(SEED+'T00:00:00Z'))/86400000);return item.forecast.map((_,j)=>item.forecast[(j+offset)%7])}
function today(){return new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Seoul',year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date())}
function trackLots(p,ds,input){
 for(const key of ['received','waste','closing'])if(input[key]===''||input[key]===null||!Number.isSafeInteger(Number(input[key]))||Number(input[key])<0)throw Error(`${p.ingredient_name}: 입고·폐기·잔량은 0 이상의 정수로 입력해주세요.`);
 const received=Number(input.received),waste=Number(input.waste),closing=Number(input.closing),consumed=p.stock+received-waste-closing;
 if(consumed<0)throw Error(`${p.ingredient_name}: 폐기와 잔량의 합계가 기존 재고와 입고의 합계를 넘습니다.`);
 const trail=p.lots.map(l=>({...l,before:l.qty,used:0,wasted:0,origin:'기존 잔량'}));
 if(received)trail.push({qty:received,before:received,used:0,wasted:0,origin:'당일 입고',received:ds,expiry:add(ds,p.shelf_life_days-1)});
 trail.sort((a,b)=>a.received.localeCompare(b.received)||a.expiry.localeCompare(b.expiry));
 let discard=waste;
 for(const l of trail){if(l.expiry>=ds)continue;const take=Math.min(l.qty,discard);l.qty-=take;l.wasted+=take;discard-=take;}
 if(trail.some(l=>l.expiry<ds&&l.qty>0))throw Error(`${p.ingredient_name}: 이미 기한이 지난 재고는 사용할 수 없습니다. 폐기 수량을 확인해주세요.`);
 let consume=consumed;
 for(const l of trail){const take=Math.min(l.qty,consume);l.qty-=take;l.used+=take;consume-=take;}
 for(const l of trail){const take=Math.min(l.qty,discard);l.qty-=take;l.wasted+=take;discard-=take;}
 if(consume>0||discard>0)throw Error(`${p.ingredient_name}: 재고 수량을 배분할 수 없습니다.`);
 if(trail.some(l=>l.expiry<=ds&&l.qty>0))throw Error(`${p.ingredient_name}: 당일 마감까지 기한이 끝나는 잔량이 있습니다. 사용·폐기를 확인하고 잔량을 수정해주세요.`);
 return {consumed,trail,lots:trail.filter(l=>l.qty>0).map(({qty,received,expiry})=>({qty,received,expiry}))};
}
function buildRecord(data,records,store,ds,inputs){
 if(ds<=SEED||!/^\d{4}-\d{2}-\d{2}$/.test(ds))throw Error('10월 1일 이후의 유효한 기록일을 선택해주세요.');
 if(Object.keys(records[store]||{}).some(d=>d>ds))throw Error('이후 기록이 있어 과거 재고를 바로 수정할 수 없습니다. 마지막 기록만 수정할 수 있습니다.');
 const previous=previousSnapshot(data,records,store,ds);if(!previous)throw Error(`이전 날짜 기록이 없습니다. ${nextDate(records,store)}부터 순서대로 기록해주세요.`);
 const seed=data.snapshots[store+'_'+SEED];
 const prepared=previous.map(p=>{
  const input=inputs[p.ingredient_id];if(!input)throw Error(`${p.ingredient_name}: 입력값이 없습니다.`);
  const tracking=trackLots(p,ds,input);
  const received=Number(input.received),waste=Number(input.waste),closing=Number(input.closing),opening=p.stock,consumed=tracking.consumed,lots=tracking.lots;
  const pending=p.pending.map(x=>({...x}));let credited=received;for(const line of pending.sort((a,b)=>a.date.localeCompare(b.date))){if(line.date>ds)continue;const take=Math.min(line.qty,credited);line.qty-=take;credited-=take;}
  const forecast=projectForecast(seed.find(x=>x.ingredient_id===p.ingredient_id),ds);
  const actual={opening_qty:opening,received_qty:received,adjust_qty:0,counted_qty:opening+received,consumed_qty:consumed,waste_qty:waste,transfer_in_qty:0,transfer_out_qty:0,closing_qty:closing,closing_value:Math.round(closing*p.price/p.use_qty_per_order_unit),receipts:received?[{po:'점장 입력',line:'manual',date:ds,packs:received/p.use_qty_per_order_unit,unit:p.order_unit,qty:received,status:'입고 확인'}]:[],waste_records:waste?[{qty:waste,reason:input.reason||'점장 확인 폐기',cost:Math.round(waste*p.price/p.use_qty_per_order_unit)}]:[]};
  return {...p,source:'manager',scenario:ds>today()||p.scenario===true,allocationMethod:'FIFO',lotTrace:tracking.trail,stock:closing,lots:lots.filter(l=>l.qty>0),pending:pending.filter(l=>l.qty>0),forecast,actual,history:[...p.history,{date:ds,qty:closing}].slice(-14)};
 });
 return {date:ds,store,source:'manager',savedAt:new Date().toISOString(),scenario:ds>today()||prepared.some(i=>i.scenario),allocation:'FIFO estimate; consumption includes count differences',items:prepared};
}
function load(){const raw=(globalThis.WorkspaceStorage || localStorage).getItem(KEY);if(!raw)return {};const parsed=JSON.parse(raw);if(parsed.version!==1||typeof parsed.records!=='object'||!parsed.records)throw Error('저장 형식을 확인할 수 없습니다.');return parsed.records;}
function save(records){(globalThis.WorkspaceStorage || localStorage).setItem(KEY,JSON.stringify({version:1,records}));}
globalThis.DailyLedger={SEED,KEY,nextDate,previousSnapshot,projectForecast,trackLots,today,buildRecord,load,save};
})();
