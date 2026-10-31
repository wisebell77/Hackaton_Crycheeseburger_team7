/* Settings policy adapter for the integrated team OrderEngine. */
(function(){
const presets={balanced:{service_level:.95,extra_demand_buffer_ratio:0,shortage_weight:1,waste_weight:1,tail_weight:.2,cvar_alpha:.9},safe:{service_level:.99,extra_demand_buffer_ratio:0,shortage_weight:3,waste_weight:1,tail_weight:.4,cvar_alpha:.9},lean:{service_level:.9,extra_demand_buffer_ratio:0,shortage_weight:1,waste_weight:3,tail_weight:.1,cvar_alpha:.9}};
const defaults=()=>({version:1,ownerText:'',mode:'balanced',preferences:{...presets.balanced},scenario:{demand_multiplier:1,delay_buffer_days:0},constraints:{budget:null,max_stockout_probability:null,max_expected_waste_won:null,allow_emergency:false,store_ids:null,excluded_ingredient_ids:[]},api:false,startDate:'2026-09-30'});
const copy=x=>JSON.parse(JSON.stringify(x));
function merge(base,patch){let p=copy(base);if(!patch)return p;if(patch.status!=='ready')throw Error('요청 해석을 먼저 확인해주세요.');for(const a of patch.audit.filter(a=>!a.mapping)){if(a.field==='mode'){p.mode=a.after;Object.assign(p.preferences,presets[a.after]);}else{let g=['preferences','scenario','constraints'].find(g=>a.field in p[g]);if(!g)continue;p[g][a.field]=a.field==='excluded_ingredient_ids'?[...new Set([...p[g][a.field],...a.after])]:copy(a.after);}}return p;}
function cvar(v,a){const x=[...v].sort((a,b)=>b-a),mass=x.length*(1-a),whole=Math.floor(mass);return (x.slice(0,whole).reduce((a,b)=>a+b,0)+(mass-whole)*(x[whole]||0))/mass;}
function evaluate(raw,context,projection,promo,policy){
 const source=raw.map(i=>({...i,pending:i.pending.map(x=>({...x,date:addDay(x.date,policy.scenario.delay_buffer_days)})),forecast:projection.ingredients[i.ingredient_id].map(v=>v*policy.scenario.demand_multiplier)}));
 FreshOrderEngine.configure({forecast:({item})=>source.find(i=>i.ingredient_id===item.ingredient_id).forecast});
 let items;try{items=FreshOrderEngine.buildItems(source,{...context,overrides:{}});}finally{FreshOrderEngine.configure({forecast:({item})=>projection.ingredients[item.ingredient_id]});}
 const menuForecast=Object.fromEntries(Object.entries(projection.menu).map(([k,values])=>[k,values.map(v=>v*policy.scenario.demand_multiplier)]));
 const ctx=OrderEngine.context({asOf:context.asOf,store:context.storeId,dates:context.dates,suppliers:context.suppliers,menuForecast,baseIngredients:context.baseIngredients||projection.ingredients,menus:promo.menus,bom:promo.bom,extraDemandBufferRatio:policy.preferences.extra_demand_buffer_ratio,excludedIngredientIds:policy.constraints.excluded_ingredient_ids});
 const presets={current:{...policy.preferences},balanced:{...policy.preferences,service_level:.95},safe:{...policy.preferences,service_level:.99},lean:{...policy.preferences,service_level:.9}};
 const result=OrderEngine.plan(items,ctx,{presets}),labels={current:'설정 기준',balanced:'균형',safe:'품절 대비',lean:'재고 절약'};
 const entries=context.quantities?[{name:'수동 수정안',qty:context.quantities,eval:OrderEngine.evaluate(items,context.quantities,ctx),recommend:result.presets.current.recommend,service_level:policy.preferences.service_level,variant:'수동 수정'}]:Object.entries(result.presets).flatMap(([key,v])=>v.candidates.map(c=>({...c,name:labels[key]+' · '+c.variant,recommend:v.recommend,service_level:v.service_level})));
 if(!context.quantities)entries.push({name:'발주 없음',qty:{},eval:result.zero,recommend:result.presets.current.recommend,service_level:policy.preferences.service_level,variant:'발주 없음'});
 const seen=new Set(),candidates=[];
 for(const c of entries){const key=JSON.stringify(items.map(i=>c.qty[i.ingredient_id]||0));if(seen.has(key))continue;seen.add(key);
  const t=c.eval.totals,violations=OrderEngine.minOrderIssues(items,c.qty,ctx).map(x=>(ctx.supplierMap[x.supplier_id]?.supplier_name||x.supplier_id)+' 최소 발주금액 미달');
  const con=policy.constraints;
  if(con.store_ids&&!con.store_ids.includes(context.storeId))violations.push('설정의 적용 매장에 포함되지 않습니다.');
  const b=con.budget;if(b&&(t.purchase_amount>b.won||b.operator==='lt'&&t.purchase_amount>=b.won))violations.push('발주 예산 초과');
  if(con.max_stockout_probability!==null&&t.p_any_stockout>con.max_stockout_probability+1e-12)violations.push('품절 확률 상한 초과');
  if(con.max_expected_waste_won!==null&&t.mean_waste_value>con.max_expected_waste_won+1e-8)violations.push('기대 폐기비 상한 초과');
  for(const i of items){const q=c.qty[i.ingredient_id]||0;if(!Number.isInteger(q)||q<0||q>0&&q<i.min_order_qty)violations.push(i.ingredient_name+' 수량 오류');if(q>0&&con.excluded_ingredient_ids.includes(i.ingredient_id))violations.push(i.ingredient_name+' 발주 제외 품목');}
  candidates.push({...c,quantities:c.qty,purchase:t.purchase_amount,prob:t.p_any_stockout,waste:t.mean_waste_value,lost:t.mean_lost_revenue,score:OrderEngine.score(c.eval,policy.preferences),feasible:!violations.length,violations});
 }
 const ranked=candidates.filter(c=>c.feasible).sort((a,b)=>a.score-b.score||a.purchase-b.purchase);
 return {candidates,ranked,best:ranked[0]||null,items,policy,ctx,engine:result};
}
function addDay(s,n){const d=new Date(s+'T00:00:00Z');d.setUTCDate(d.getUTCDate()+n);return d.toISOString().slice(0,10);}
globalThis.OrderPolicy={defaults,presets,merge,cvar,evaluate};
})();
