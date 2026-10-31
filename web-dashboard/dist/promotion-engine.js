/* Same explicit response × menu baseline × BOM contract as promotion_forecast/core.py. */
(function(){
function validate(plans,data,asof,store,dates){
 const menus=new Map(data.menus.map(m=>[m.menu_id,m])),w=data.windows[asof];
 for(const p of plans){
 if(!p.name?.trim()||!p.promotion_id||p.store_ids?.length!==1||p.store_ids[0]!==store)throw Error('행사 이름과 적용 매장을 확인해주세요.');
 if(!Array.isArray(p.target_menu_ids)||!p.target_menu_ids.length||new Set(p.target_menu_ids).size!==p.target_menu_ids.length||p.target_menu_ids.some(m=>!menus.has(m)))throw Error('등록된 대상 메뉴를 한 개 이상 선택해주세요.');
 if(!dates.includes(p.start_date)||!dates.includes(p.end_date)||p.start_date>p.end_date)throw Error('행사 기간은 이번 예측 7일 안에서 시작일 ≤ 종료일로 선택해주세요.');
 const offer=p.offer;if(p.kind==='special'){if(offer?.type!=='none'||offer.value!==0||p.response?.mode!=='manual')throw Error('일반 특이사항은 수동 수요 변화율을 입력해주세요.');}else{if(!Number.isFinite(offer?.value)||offer.value<=0||!['percent_off','amount_off'].includes(offer.type))throw Error('할인 조건을 입력해주세요.');
 if(offer.type==='percent_off'&&offer.value>100||offer.type==='amount_off'&&(!Number.isInteger(offer.value)||p.target_menu_ids.some(m=>menus.get(m).virtual_price<offer.value)))throw Error('정률은 100% 이하, 정액은 메뉴 가격 이하의 정수 원으로 입력해주세요.');}
 if(p.response?.mode==='manual'){
 if(!Number.isFinite(p.response.uplift_pct)||p.response.uplift_pct< -100||p.response.uplift_pct>200||!p.response.reason?.trim())throw Error('예상 수요 변화율(-100~200%)과 근거를 모두 입력해주세요.');
 }else if(p.response?.mode==='historical_reference'){
 const ref=w.references.find(r=>r.id===p.response.reference_promo_id);
 if(!ref||ref.error||ref.store!==store||p.response.accept_transfer_assumption!==true||[...ref.menus].sort().join()!==[...p.target_menu_ids].sort().join())throw Error('과거 행사 참고는 동일 매장·동일 메뉴와 전이 가정 확인이 필요합니다.');
 }else throw Error('예상 수요 반응을 선택해주세요.');
 for(const mid of p.target_menu_ids)for(let d=0;d<dates.length;d++)if(dates[d]>=p.start_date&&dates[d]<=p.end_date&&w.stores[store][mid]?.available[d]!==1)throw Error('행사 기간에 취급할 수 없는 메뉴가 있습니다.');
 }
 if(new Set(plans.map(p=>p.promotion_id)).size!==plans.length)throw Error('행사 ID가 중복됩니다.');
 for(let i=0;i<plans.length;i++)for(let j=i+1;j<plans.length;j++)if(plans[i].target_menu_ids.some(m=>plans[j].target_menu_ids.includes(m))&&plans[i].start_date<=plans[j].end_date&&plans[j].start_date<=plans[i].end_date)throw Error('같은 메뉴·날짜의 행사는 중복 적용할 수 없습니다.');
 return plans;
}
function project(data,asof,store,dates,plans){
 validate(plans,data,asof,store,dates);const w=data.windows[asof];
 const menu={};for(const [mid,v] of Object.entries(w.stores[store]))menu[mid]=v.qty.map((q,d)=>{const p=plans.find(p=>p.target_menu_ids.includes(mid)&&dates[d]>=p.start_date&&dates[d]<=p.end_date);if(!p)return q;const factor=p.response.mode==='manual'?1+p.response.uplift_pct/100:w.references.find(r=>r.id===p.response.reference_promo_id).factors[mid].factor;return q*factor;});
 const ingredients={};for(const b of data.bom){ingredients[b.ingredient_id]??=Array(7).fill(0);for(let d=0;d<7;d++)ingredients[b.ingredient_id][d]+=(menu[b.menu_id]?.[d]||0)*b.qty_per_menu;}
 return {menu,ingredients};
}
globalThis.PromotionEngine={validate,project};
})();
