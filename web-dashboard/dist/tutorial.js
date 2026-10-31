/* Guided practice on the real dashboard. Business calculations are unchanged. */
(function () {
  'use strict';
  const KEY = 'fresh-order-tutorial-v1';
  const chapters = ['발주 살펴보기', '설정과 요청', '행사와 발주서', '재고 마감', '이력과 기준'];
  const copy = value => JSON.parse(JSON.stringify(value));
  const q = selector => document.querySelector(selector);
  let available = false, active = false, index = 0, snapshot = null, layer, banner, welcome;
  let frame = 0, moving = false, transition = 0, actionBusy = false, previouslyFocused, actionTarget = null;
  const readProgress = () => { try { return JSON.parse(localStorage.getItem(KEY) || '{}') || {}; } catch { return {}; } };
  function remember(values) { try { localStorage.setItem(KEY, JSON.stringify({...readProgress(), ...values})); } catch { /* Tour also works without browser storage. */ } }

  function closeSurfaces() {
    // Move the guide out before renderSettings replaces the dialog's children.
    if (layer) document.body.append(layer);
    document.querySelectorAll('dialog[open]').forEach(d => { if (d !== welcome) d.close(); });
    if (q('#drawer') && !q('#drawer').hidden) closeDrawer();
  }
  function overview() { closeSurfaces(); state.nav = 'overview'; state.date = DailyLedger.SEED; state.filter = 'all'; state.search = ''; q('#dateSelect').value = state.date; q('#search').value = ''; render(); }
  function settings(tab) {
    if (layer) document.body.append(layer);
    if (!q('#settingsDialog')?.open) { closeSurfaces(); openSettings(); }
    else { try { collectSettings(); } catch { /* Explain invalid fields without blocking navigation. */ } }
    settingsTab = tab; renderSettings();
  }
  function daily(ds = DailyLedger.SEED) { closeSurfaces(); state.nav = 'daily'; state.date = ds; state.filter = 'all'; state.search = ''; q('#dateSelect').value = ds; render(); }
  async function history() {
    closeSurfaces(); state.nav = 'history'; state.date = DailyLedger.SEED; q('#dateSelect').value = state.date;
    Object.assign(historyState, {from:'2026-09-01',to:'2026-09-30',supplier:'all',ingredient:'all',status:'all',type:'all',query:'',page:1});
    render(); await renderPurchaseHistory();
  }
  function makeStep(chapter, title, body, target, enter, extra = {}) { return {chapter, title, body, target, enter, ...extra}; }
  const steps = [
    makeStep(0, '매장과 날짜부터 정합니다', '매장 A와 9월 30일 마감 재고로 시작합니다. 선택한 날짜 다음 날부터 7일간의 소요량을 계산합니다. 다른 매장을 누르면 그 매장의 재고와 발주안으로 바뀝니다.', '.controls', overview, {hint:'기준일은 오늘 날짜가 아니라, 재고를 마감한 날짜입니다.'}),
    makeStep(0, '부족과 폐기 위험을 구분합니다', '부족 예상은 추가 발주 없이 버텼을 때 모자라는 품목입니다. 폐기 위험은 사용기한 안에 쓰지 못할 물량이 있는 경우입니다. 과잉은 현재 재고가 7일 소요량보다 많은 상태입니다.', '#metrics', overview, {hint:'지금 재고에 대한 표시입니다. 발주 후 위험은 품목 상세에서 따로 비교합니다.'}),
    makeStep(0, '필요한 품목만 찾아봅니다', '검색창에 식자재 이름을 입력하거나 부족 예상·폐기 위험·과잉 필터를 누르세요. 재고와 소요량은 개·kg·L로, 발주 수량은 박스·팩 등 포장 단위로 표시됩니다.', '#inventoryPanel .inventory-heading', overview, {hint:'예상 소요량 전체가 발주량이 되는 것은 아닙니다. 남은 재고와 입고 예정량을 빼고 계산합니다.', example:'부족 예상 품목만 보기', action:() => { state.filter='short'; renderTable(); return '목록에서 부족 예상 품목만 남겼습니다. 전체를 누르면 모든 품목이 보입니다.'; }}),
    makeStep(0, '이 수량을 추천한 이유를 봅니다', '소고기 패티를 열었습니다. 입고 일정, 이번 발주가 맡을 기간, 안전재고, 가용 재고와 기존 미입고량을 확인할 수 있습니다. 위쪽 표에서는 신규 발주 전과 현재 초안의 부족량·기한 경과 잔량을 비교합니다.', '#drawer .drawer-header', () => { overview(); openItem('I001'); }, {hint:'상세 패널을 아래로 내리면 과거 오차 시나리오로 계산한 위험도 볼 수 있습니다.'}),
    makeStep(0, '재고가 줄어드는 흐름을 봅니다', '실선은 과거 마감 재고, 점선은 예상 재고입니다. 품목 선택을 바꾸면 해당 식자재의 흐름이 나옵니다. 오른쪽 우선 확인 품목을 눌러서도 상세 근거를 열 수 있습니다.', '#analysisRow', overview, {hint:'예상 그래프는 신규 발주 전 전망이며, 이미 주문한 입고 예정 물량은 반영합니다.'}),

    makeStep(1, '평소 발주 성향을 고릅니다', '균형은 품절과 폐기를 함께 보고, 품절 대비는 판매가 막히는 손실을 더 크게 봅니다. 재고 절약은 폐기 비용에 더 무게를 둡니다. 버튼을 누르면 아래 계산 기준이 바뀝니다.', '#settingsDialog .preset-buttons', () => { overview(); settings('profile'); }, {hint:'직접 선택하고 저장해도 됩니다. 튜토리얼에서 저장한 설정은 연습이 끝나면 사라집니다.'}),
    makeStep(1, '계산 기준을 직접 바꿉니다', '서비스 수준과 추가 여유율은 확보할 수량에, 품절·폐기 가중치는 후보 비교에 쓰입니다. 큰 손실 대비 비중과 CVaR은 손실이 큰 경우를 얼마나 볼지 정합니다. 전체 수요 배율과 기존 입고 지연일도 여기서 조정합니다.', '#settingsDialog .settings-fields', () => settings('parameters'), {hint:'서비스 수준 99%가 실제 품절 방지율 99%를 뜻하지는 않습니다. 지연일은 기존 미입고 건에만 적용합니다.'}),
    makeStep(1, '예산과 제외 품목을 정합니다', '예산은 미만·이하를 구분합니다. 부족 시나리오 비율과 기대 폐기비에 상한을 둘 수도 있습니다. 아래의 제외 품목은 새로 주문하지 않지만, 해당 재료의 수요와 부족 위험은 그대로 계산합니다.', '#settingsDialog #settingBudget', () => settings('constraints'), {hint:'적용 매장과 제외 품목은 같은 탭 아래에 있습니다. 예산은 현재 선택한 매장 기준이며, 매장 합산 예산은 지원하지 않습니다.'}),
    makeStep(1, '설정을 보관하고 다시 불러옵니다', '시작 기준일을 정하고 설정 파일을 내보내거나 가져올 수 있습니다. 기본 설정 초기화도 여기 있습니다. 평소 설정은 이 브라우저에 저장되며 다른 기기에 자동 공유되지 않습니다.', '#settingsDialog .settings-content', () => settings('storage'), {hint:'연습 화면에서도 설정 파일은 내려받을 수 있습니다. 원래 저장된 설정 파일과 구분해 보관하세요.'}),
    makeStep(1, '이번 발주에만 조건을 붙입니다', '평소 설정을 바꾸지 않고 이번 발주만 조정할 때 씁니다. “10% 여유, 우유 제외”처럼 적고 요청 반영을 누르세요. 해석된 항목을 확인한 뒤 진행하며, 이번 요청 해제로 기본 설정으로 돌아갑니다.', '#quickOrder .request-box', overview, {hint:'자유 문장 해석은 설정에서 켤 수 있습니다. 해석 오류가 나면 문장을 고치거나 숫자로 직접 설정하세요.', example:'예시 요청 반영하기', action:async() => { const text='10% 여유, 우유 제외', patch=await extractRequest(text,false); if(!active)return; orderPatch=patch;orderRequest=text;requestDirty=false;state.overrides={};policyCache='';render();return '추가 여유율 10%와 우유 제외를 반영했습니다. 추천 금액과 품목이 달라졌는지 살펴보세요.'; }}),
    makeStep(1, '실제로 적용된 기준을 확인합니다', '설정과 이번 요청을 합친 최종 기준입니다. 이번 요청에 적은 항목이 기본 설정보다 우선합니다. 요청을 바꾼 뒤에는 이 화면에서 예산·제외 품목·수요 배율이 의도한 값인지 확인하세요.', '#drawer .drawer-header', () => { overview(); q('#showPolicy').click(); }),

    makeStep(2, '다른 발주안과 비교합니다', '후보별 발주금액, 부족 시나리오 비율, 폐기비와 손실매출을 비교합니다. 같은 기준으로 매긴 비교 점수가 작을수록 현재 설정에 맞는 안입니다. 조건을 지키지 못한 후보는 선택할 수 없습니다.', '#orderPlanCards .comparison-grid', () => { overview(); q('.alternative-plans').open=true; }, {hint:'화면의 최적은 비교한 후보 안에서의 선택입니다. 실제 미래 수요를 보장하는 값은 아닙니다.'}),
    makeStep(2, '행사나 매장 특이사항을 입력합니다', '행사 추가를 열었습니다. 종류·이름·기간·할인 조건과 대상 메뉴를 지정합니다. 일반 특이사항도 입력할 수 있습니다. 세트와 단품은 따로 선택하며, 적용 기간은 이번 예측 7일 안입니다.', '#promotionForm .promo-form-grid', () => { overview(); q('#promotionEditor').open=true; }, {hint:'할인율과 예상 수요 변화율은 서로 다른 값입니다. 할인 10%를 입력해도 수요가 자동으로 10% 늘지는 않습니다.'}),
    makeStep(2, '수요 변화가 재료에 미치는 영향을 봅니다', '예상 수요 반응을 직접 입력하거나 같은 매장의 과거 행사 참고값을 선택합니다. 미리보기를 누르면 메뉴 수요와 식자재 소요량의 적용 전후가 나옵니다. 확인한 뒤 조건 적용을 눌러야 발주안에 반영됩니다.', '#upliftField', () => { overview(); q('#promotionEditor').open=true; }, {hint:'같은 메뉴·날짜에 행사를 중복 적용할 수 없습니다. 메모는 설명용이며 AI가 수요 변화율로 바꾸지 않습니다.', afterActionTarget:'#promoPreview', example:'치즈버거 세트 수요 +30% 미리보기', action:() => {
      const menu=PROMO.menus.find(m=>m.is_set&&PROMO.windows[state.date].stores[state.store][m.menu_id]?.available.some(Boolean));
      if(!menu)throw Error('이 매장에서 참고할 세트 메뉴가 없습니다.');
      q('#promoName').value='튜토리얼 세트 행사';q('#promoValue').value='10';q('#promoUplift').value='30';
      q('#promoReason').value='연습용 수요 증가 가정';q('[name="promoMenu"][value="'+menu.menu_id+'"]').checked=true;
      q('#promotionForm').requestSubmit();if(q('#promoPreview').hidden)throw Error(q('#promoError').textContent);
      return '10% 할인과 수요 +30%를 따로 입력했습니다. 아래 미리보기에서 재료별 변화량을 확인하세요.';
    }}),
    makeStep(2, '행사 조건을 반영해 다시 계산합니다', '조건 적용을 누르면 대상 메뉴의 수요와 식자재 소요량, 추천 발주량을 함께 다시 계산합니다. 적용 중인 행사의 해제를 누르면 해당 조건을 뺍니다. 행사 조건을 바꾸면 수동으로 수정한 발주 수량은 초기화됩니다.', '#promotionSection', () => { overview(); q('#promotionEditor').open=true; }, {hint:'앞 단계의 미리보기는 화면 이동 시 닫힙니다. 아래 버튼으로 예시 행사를 적용해 결과를 확인할 수 있습니다.', afterActionTarget:'#appliedPromotions', example:'예시 행사 적용하기', action:() => {
      const menu=PROMO.menus.find(m=>m.is_set&&PROMO.windows[state.date].stores[state.store][m.menu_id]?.available.some(Boolean));
      if(!menu)throw Error('이 매장에서 참고할 세트 메뉴가 없습니다.');
      const w=forecastWindow();savePromotionPlans([{promotion_id:'TUTORIAL_PROMO',name:'연습용 세트 행사',kind:'promotion',store_ids:[state.store],target_menu_ids:[menu.menu_id],start_date:w.dates[0],end_date:w.dates[6],offer:{type:'percent_off',value:10},response:{mode:'manual',uplift_pct:30,reason:'튜토리얼 연습용 가정'}}]);
      return '예시 행사를 반영했습니다. 추천 금액과 품목별 소요량을 확인하세요. 실제 행사 기록에는 저장되지 않습니다.';
    }}),
    makeStep(2, '필요하면 품목 수량을 고칩니다', '발주 수량 칸에서 포장 수량을 직접 바꾸세요. 금액과 초안의 위험도 다시 계산합니다. 수량 초기화를 누르면 추천 수량으로 돌아갑니다. 최소 주문수량·공급사 최소금액·예산은 저장 전에 다시 검사합니다.', '#inventoryBody [data-order-input="I001"]', overview, {hint:'개나 kg의 소요량이 아니라, 오른쪽에 표시된 박스·팩 수량을 입력합니다.', afterActionTarget:'#inventoryBody [data-item="I001"]', example:'패티 한 박스 추가해 보기', action:() => { const item=items.find(i=>i.ingredient_id==='I001');state.overrides.I001=item.orderQty+1;render();return '패티 한 박스를 추가했습니다. 품목 금액과 상단 총액을 확인하세요.'; }}),
    makeStep(2, '발주서를 마지막으로 확인합니다', '선택한 품목, 수량, 금액과 공급사별 입고일을 확인합니다. 발주서 다운로드는 검토용 CSV를 만듭니다. 이 버튼을 눌러도 공급사에 주문이 전송되거나 과거 발주 이력에 추가되지는 않습니다.', '#drawer #exportDraft, #optimalOrder', () => { overview();reviewOrders(); }, {hint:'직접 고친 안도 조건 검사를 통과해야 내려받을 수 있습니다. 검토 창이 열리지 않으면 수량·예산 조건이나 반영하지 않은 요청을 확인하세요.'}),

    makeStep(3, '실제 재고 기록을 확인합니다', '일일 재고 기록은 실제 입고·소진·폐기·마감 잔량을 보여줍니다. 당일 입고·폐기·기한 임박 필터와 검색을 사용할 수 있고, 마감 내역도 내려받을 수 있습니다. 지금 보이는 9월 30일은 제공된 기록입니다.', '#dailyInventory .daily-toolbar', () => daily(), {hint:'기한 임박은 곧 폐기된다는 뜻이 아닙니다. 사용기한 안에 소진할 수 있는지 확인하세요.'}),
    makeStep(3, '입고 묶음마다 기한을 봅니다', '품목을 열면 실제 입고 내역과 재고 수지, 입고 묶음별 잔량·입고일·마지막 사용일을 볼 수 있습니다. 이후 직접 입력한 기록의 묶음 잔량은 선입선출 가정으로 추정하며, 제공된 실제 묶음 기록과 구분합니다.', '#drawer .lot-tracking', () => { daily();openDailyItem('I001'); }),
    makeStep(3, '다음 날의 실제 수량을 입력합니다', '10월 1일 입력 화면입니다. 실제 입고량·폐기량·마감 잔량을 입력하고, 폐기 사유를 선택합니다. 입고 예정량은 자동으로 현재고에 더하지 않습니다. 변동 없는 품목은 전일 잔량으로 채우기를 이용할 수 있습니다.', '#dailyInventory .entry-table tbody tr', () => daily('2026-10-01'), {hint:'입력은 g·ml·개 같은 사용 단위입니다. 박스나 kg를 그대로 넣지 마세요. 잔량·사용기한 추적으로 묶음별 배분도 미리 볼 수 있습니다.'}),
    makeStep(3, '마감 기록을 저장해 봅니다', '수량을 확인한 뒤 확인란에 체크하고 마감 재고 저장을 누릅니다. 저장한 잔량이 다음 발주 계산의 기준이 됩니다. 날짜를 건너뛸 수 없고, 기한이 끝난 잔량은 사용·폐기를 확인해야 합니다.', '#dailyInventory .entry-save-bar', () => daily('2026-10-01'), {hint:'아래 예시는 기한이 끝난 물량을 폐기하고 나머지 재고의 일부를 소진한 연습용 기록입니다.', afterActionTarget:'#dailyInventory .entry-saved-actions', example:'예시 마감 기록 저장해 보기', action:() => {
      const prev=DailyLedger.previousSnapshot(DATA,managerRecords,state.store,state.date);
      for(const item of prev){const row=q('[data-entry="'+item.ingredient_id+'"]'),waste=item.lots.filter(l=>l.expiry<=state.date).reduce((s,l)=>s+l.qty,0);row.querySelector('[data-field="received"]').value=0;row.querySelector('[data-field="waste"]').value=waste;row.querySelector('[data-field="closing"]').value=Math.floor((item.stock-waste)*.75);row.querySelector('[data-field="reason"]').value='소비기한 경과';}
      q('#confirmEntry').checked=true;q('#saveDailyRecord').click();
      if(!managerRecords[state.store]?.[state.date])throw Error(q('#entryError')?.textContent||'입력 수량을 확인해주세요.');
      return '연습 기록을 저장했습니다. 입력값 수정·다음 날 기록·기록 백업 버튼을 사용할 수 있습니다. 원래 재고 기록에는 반영되지 않습니다.';
    }}),
    makeStep(3, '기록을 이어가거나 백업합니다', '저장한 기록에서는 입력값 수정, 다음 날 기록, 기록 백업을 사용할 수 있습니다. 이후 날짜가 이미 저장되어 있으면 앞 날짜는 바로 수정할 수 없습니다. 발주 관리로 돌아가 같은 기준일의 변경된 발주안을 확인할 수 있습니다.', '#dailyInventory .entry-saved-actions', () => daily(managerRecords[state.store]?.['2026-10-01']?'2026-10-01':DailyLedger.SEED), {hint:'앞 단계에서 예시를 저장하면 세 버튼이 나타납니다. 직접 입력한 기록은 이 브라우저에만 남으며, 백업은 JSON 파일로 내려받습니다.'}),

    makeStep(4, '과거 발주를 조건별로 찾습니다', '매장과 조회 기준일을 정한 뒤 발주 기간·공급사·식자재·입고 상태·정기 또는 긴급 유형을 골라 조회합니다. 발주 번호나 품목명으로 검색할 수 있고, 조회 결과가 많으면 페이지를 넘겨봅니다.', '#purchaseHistory .history-controls', history, {hint:'날짜가 바뀌면 그날까지 확인된 입고 기록만 상태 판정에 사용합니다.'}),
    makeStep(4, '예정 입고와 실제 입고를 구분합니다', '이력의 품목을 열면 발주 수량·단가·예정일과 기준일까지의 실제 입고를 비교할 수 있습니다. 9월 30일 이후의 실제 입고 기록이 없는 건은 곧바로 지연이나 입고 실패로 단정하지 않습니다.', '#drawer .drawer-header', async() => { const scope=WorkspaceStorage.generation;await history();if(active&&scope===WorkspaceStorage.generation&&historyRows.length)openHistory(historyRows[0].id); }, {hint:'이 목록은 제공된 과거 기록입니다. 튜토리얼이나 발주 신청에서 만든 초안은 여기에 추가되지 않습니다.'}),
    makeStep(4, '조회 결과를 내려받습니다', '현재 기간과 필터가 적용된 이력을 CSV로 저장합니다. 서로 단위가 다른 품목의 수량은 한 숫자로 합치지 않으며, 합계 금액은 가상 가격 기준입니다.', '#historyExport', history),
    makeStep(4, '데이터와 계산 가정을 확인합니다', '데이터 기준에는 관측 기간, 예측식, 단위, 발주 조건과 계산 범위가 정리되어 있습니다. 제공 자료는 합성 데이터이고 가격은 가상 값입니다. 판매 관측은 9월 30일까지이며, 발주 예측은 10월 14일 기준일까지 준비되어 있습니다.', '#methodDialog h2', () => { overview(); q('#methodDialog').showModal(); }, {hint:'실제 공급사 주문 전송과 기기 간 기록 공유는 지원하지 않습니다. 실제 발주 결정은 담당자가 내립니다.'})
  ];

  function capture() {
    return copy({state, preferences, orderPatch, orderRequest, requestDirty, policySelection, managerRecords, promotionRecords, ledgerError, promotionLoadWarning, dailyState, historyState});
  }
  function resetPractice() {
    Object.assign(state,{store:'S01',date:DailyLedger.SEED,filter:'all',search:'',nav:'overview',selected:null,plan:'balanced',overrides:{}});
    preferences=OrderPolicy.defaults();orderPatch=null;orderRequest='';requestDirty=false;policySelection=null;policyCache='';
    managerRecords={};promotionRecords={};ledgerError='';promotionLoadWarning='';Object.assign(dailyState,{filter:'all',query:''});
  }
  function restore() {
    closeSurfaces();WorkspaceStorage.endPractice();
    Object.assign(state,snapshot.state);preferences=snapshot.preferences;orderPatch=snapshot.orderPatch;orderRequest=snapshot.orderRequest;requestDirty=snapshot.requestDirty;policySelection=snapshot.policySelection;
    managerRecords=snapshot.managerRecords;promotionRecords=snapshot.promotionRecords;ledgerError=snapshot.ledgerError;promotionLoadWarning=snapshot.promotionLoadWarning;
    Object.assign(dailyState,snapshot.dailyState);Object.assign(historyState,snapshot.historyState);policyCache='';
    q('#dateSelect').value=state.date;q('#search').value=state.search;
    // calculatePolicy clears selection on a cache miss; restore after the first calculation.
    const selected=snapshot.policySelection;render();if(selected){policySelection=selected;render();}
  }
  function finish(completed=false) {
    if(!active)return;active=false;transition++;moving=false;actionBusy=false;cancelAnimationFrame(frame);frame=0;
    remember({offered:true,completed:completed||readProgress().completed,lastStep:Math.min(index,steps.length-1)});
    restore();layer?.remove();banner?.remove();layer=null;banner=null;document.body.classList.remove('tutorial-active');snapshot=null;
    previouslyFocused?.isConnected?previouslyFocused.focus():q('#tutorialLaunch')?.focus();
    toast(completed?'튜토리얼을 마쳤습니다. 원래 화면으로 돌아왔습니다.':'튜토리얼을 닫고 원래 화면으로 돌아왔습니다.');
  }
  function start(at=0) {
    if(!available||active)return;previouslyFocused=document.activeElement;snapshot=capture();welcome?.close();closeSurfaces();
    WorkspaceStorage.beginPractice();resetPractice();active=true;document.body.classList.add('tutorial-active');
    layer=document.createElement('div');layer.id='tutorialLayer';layer.className='tutorial-layer';
    banner=document.createElement('div');banner.className='tutorial-practice';banner.innerHTML='<b>튜토리얼 연습 중</b><span>원래 기록은 유지됩니다</span><button type="button">끝내기</button>';
    banner.querySelector('button').onclick=()=>finish();document.body.append(banner,layer);
    remember({offered:true});showStep(Math.max(0,Math.min(at,steps.length-1)));
  }
  function host() {
    const modal=[...document.querySelectorAll('dialog[open]')].find(d=>d!==welcome);
    const parent=modal||document.body;
    if(layer?.parentElement!==parent)parent.append(layer);
    // The banner must share the dialog's top layer to remain visible and interactive.
    if(banner?.parentElement!==parent)parent.append(banner);
  }
  function renderCard(step) {
    layer.innerHTML='<div class="tutorial-spotlight" aria-hidden="true"></div><section class="tutorial-card" role="region" aria-label="튜토리얼 안내"><div class="tutorial-card-head"><span>'+ (index+1)+' / '+steps.length+' · '+chapters[step.chapter]+'</span><button type="button" class="tutorial-close" aria-label="튜토리얼 끝내기">×</button></div><progress aria-label="튜토리얼 진행" max="'+steps.length+'" value="'+(index+1)+'"></progress><div class="tutorial-chapters">'+chapters.map((c,k)=>'<button type="button" data-tour-chapter="'+k+'" aria-current="'+(step.chapter===k)+'">'+c+'</button>').join('')+'</div><h2 id="tutorialTitle" tabindex="-1">'+step.title+'</h2><p>'+step.body+'</p>'+(step.hint?'<p class="tutorial-hint">'+step.hint+'</p>':'')+(step.action?'<button type="button" class="tutorial-example">'+step.example+' ↗</button>':'')+'<p class="tutorial-feedback" role="status" hidden></p><div class="tutorial-actions"><button type="button" class="button secondary" id="tutorialPrev" '+(index===0?'disabled':'')+'>이전</button><button type="button" class="button primary" id="tutorialNext">'+(index===steps.length-1?'마무리 보기':'다음')+' →</button></div></section>';
    layer.querySelector('.tutorial-close').onclick=()=>finish();
    q('#tutorialPrev').onclick=()=>showStep(index-1);q('#tutorialNext').onclick=()=>index===steps.length-1?showFinish():showStep(index+1);
    layer.querySelectorAll('[data-tour-chapter]').forEach(b=>b.onclick=()=>showStep(steps.findIndex(s=>s.chapter===+b.dataset.tourChapter)));
    if(step.action)layer.querySelector('.tutorial-example').onclick=async() => {
      if(actionBusy)return;actionBusy=true;const button=layer.querySelector('.tutorial-example'),token=transition;button.disabled=true;
      try{const message=await step.action();if(active&&token===transition){actionTarget=step.afterActionTarget||null;const feedback=layer.querySelector('.tutorial-feedback');feedback.hidden=false;feedback.textContent=message||'화면에서 결과를 확인하세요.';reposition(true);}}
      catch(e){if(active&&token===transition){const feedback=layer.querySelector('.tutorial-feedback');feedback.hidden=false;feedback.textContent=e.message;}}
      finally{if(token===transition)actionBusy=false;if(button.isConnected)button.disabled=false;}
    };
  }
  async function showStep(next) {
    if(!active||moving||actionBusy)return;moving=true;actionTarget=null;const token=++transition;index=Math.max(0,Math.min(next,steps.length-1));
    try {
      await steps[index].enter();if(!active||token!==transition)return;
      host();renderCard(steps[index]);remember({lastStep:index});reposition(true);q('#tutorialTitle').focus({preventScroll:true});
    } catch(e) {
      if(active&&token===transition){host();renderCard({...steps[index],body:'이 화면을 여는 중 문제가 생겼습니다. 다음 단계로 이동하거나 튜토리얼을 다시 시작할 수 있습니다.',hint:e.message,action:null});reposition();}
      console.warn('Tutorial step:',e);
    } finally {if(token===transition)moving=false;}
  }
  function visibleTarget() {
    const candidates=document.querySelectorAll(actionTarget||steps[index]?.target||'.controls');
    // Prefer a panel in an open drawer when the selector also includes a fallback button.
    const drawerTarget=[...candidates].find(el=>el.closest('#drawer')&&!q('#drawer').hidden&&el.getClientRects().length);
    if(drawerTarget)return drawerTarget;
    return [...candidates].find(el=>el.getClientRects().length&&getComputedStyle(el).visibility!=='hidden')||null;
  }
  function reposition(scroll=false) {
    if(!active||!layer)return;host();const card=layer.querySelector('.tutorial-card'),spot=layer.querySelector('.tutorial-spotlight');if(!card||!spot)return;
    const target=visibleTarget(),mobile=window.innerWidth<=650,pad=12,vw=document.documentElement.clientWidth,vh=window.innerHeight;
    if(mobile)card.style.width=Math.max(0,vw-20)+'px';else card.style.width='';
    if(target&&scroll){const alignStart=mobile||target.offsetHeight>vh*.6;target.scrollIntoView({block:alignStart?'start':'center',inline:'nearest',behavior:'instant'});if(alignStart&&!target.closest('dialog,#drawer'))window.scrollBy(0,-90);}
    const r=target?.getBoundingClientRect();spot.hidden=!r;
    if(r){const top=Math.max(38,r.top-5),bottom=Math.min(vh-8,r.bottom+5);Object.assign(spot.style,{top:top+'px',left:Math.max(4,r.left-5)+'px',width:Math.max(0,Math.min(vw-4,r.right+5)-Math.max(4,r.left-5))+'px',height:Math.max(0,bottom-top)+'px'});}
    const w=card.offsetWidth||350,h=card.offsetHeight||320;
    if(mobile){Object.assign(card.style,{left:'10px',top:Math.max(46,vh-h-10)+'px'});return;}
    let x=vw-w-20,y=vh-h-20;
    if(r){const candidates=[{x:r.right+18,y:r.top},{x:r.left-w-18,y:r.top},{x:r.left,y:r.bottom+18},{x:r.left,y:r.top-h-18}];
      const fit=candidates.find(p=>p.x>=pad&&p.x+w<=vw-pad&&p.y>=46&&p.y+h<=vh-pad);
      if(fit){x=fit.x;y=fit.y;}else if(r.left>vw/2)x=20;
    }
    Object.assign(card.style,{left:Math.max(pad,Math.min(x,vw-w-pad))+'px',top:Math.max(46,Math.min(y,vh-h-pad))+'px'});
  }
  function showFinish() {
    closeSurfaces();overview();host();
    layer.innerHTML='<section class="tutorial-card" role="region" aria-label="튜토리얼 완료"><div class="tutorial-card-head"><span>전체 안내 완료</span><button class="tutorial-close" type="button" aria-label="튜토리얼 끝내기">×</button></div><h2 tabindex="-1" id="tutorialTitle">이제 직접 확인해 보세요</h2><p>매장과 날짜를 고르고, 조건을 바꿔 발주안을 비교해 보세요. 실제 주문은 초안을 확인한 뒤 별도로 진행합니다.</p><p class="tutorial-hint">연습 중 입력한 설정·행사·재고는 남기지 않습니다. 종료하면 튜토리얼을 시작하기 전 화면으로 돌아갑니다. 상단 사용 안내에서 언제든 다시 볼 수 있습니다.</p><div class="tutorial-finish-links">'+chapters.map((c,k)=>'<button type="button" data-tour-chapter="'+k+'">'+c+' 다시 보기</button>').join('')+'</div><div class="tutorial-actions"><button type="button" class="button primary" id="tutorialDone">마치고 원래 화면으로 →</button></div></section>';
    layer.querySelector('.tutorial-close').onclick=()=>finish(true);q('#tutorialDone').onclick=()=>finish(true);
    layer.querySelectorAll('[data-tour-chapter]').forEach(b=>b.onclick=()=>showStep(steps.findIndex(s=>s.chapter===+b.dataset.tourChapter)));
    const card=layer.querySelector('.tutorial-card');card.style.left=Math.max(10,(innerWidth-card.offsetWidth)/2)+'px';card.style.top=Math.max(50,(innerHeight-card.offsetHeight)/2)+'px';
    q('#tutorialTitle').focus({preventScroll:true});remember({completed:true});
  }
  function offer() {
    if(!available||active)return;
    if(!welcome){welcome=document.createElement('dialog');welcome.id='tutorialWelcome';welcome.className='tutorial-welcome';welcome.setAttribute('aria-labelledby','tutorialWelcomeTitle');document.body.append(welcome);}
    const progress=readProgress(),resume=!progress.completed&&Number.isInteger(progress.lastStep)&&progress.lastStep>0;
    welcome.innerHTML='<div class="tutorial-welcome-head"><button type="button" class="tutorial-close" aria-label="안내 닫기">×</button><span class="tutorial-eyebrow">처음 사용하시나요?</span><h2 id="tutorialWelcomeTitle">튜토리얼을 시작할까요?</h2><p>발주안을 확인하는 것부터 재고를 마감하는 것까지,<br>실제 화면에서 하나씩 살펴봅니다.</p></div><ol class="tutorial-route">'+chapters.map((c,k)=>'<li><span class="tutorial-route-num">'+(k+1)+'</span><div><b>'+c+'</b><small>'+['매장·기준일과 추천 근거 확인','성향·예산·제외 품목과 이번 요청','수요 변화 반영부터 초안 다운로드까지','실제 입고·폐기·잔량 입력과 백업','과거 발주·입고 조회와 데이터 가정'][k]+'</small></div></li>').join('')+'</ol><p class="tutorial-welcome-note">연습 중 바꾼 값은 원래 기록에 저장되지 않습니다.<br>순서대로 보거나 필요한 항목으로 바로 이동할 수 있습니다.</p><div class="tutorial-welcome-actions"><button type="button" class="button secondary" id="tutorialSkip">바로 둘러보기</button>'+(resume?'<button type="button" class="button secondary" id="tutorialRestart">처음부터</button>':'')+'<button type="button" class="button primary" id="tutorialStart">'+(resume?'이어서 보기':'튜토리얼 시작')+' →</button></div>';
    const skip=()=>{remember({offered:true});welcome.close();q('#tutorialLaunch')?.focus();};
    welcome.querySelector('.tutorial-close').onclick=skip;q('#tutorialSkip').onclick=skip;
    q('#tutorialStart').onclick=()=>start(resume?progress.lastStep:0);if(q('#tutorialRestart'))q('#tutorialRestart').onclick=()=>start(0);
    welcome.oncancel=()=>remember({offered:true});welcome.showModal();q('#tutorialStart').focus({preventScroll:true});
  }
  function ready() {
    if(available)return;available=true;
    const button=document.createElement('button');button.id='tutorialLaunch';button.className='tutorial-launch';button.type='button';button.textContent='? 사용 안내';button.onclick=offer;
    q('.top-right').prepend(button);
    if(!readProgress().offered&&!document.querySelector('dialog[open]'))offer();
    const observer=new MutationObserver(records=>{
      if(!active||moving||records.every(r=>r.target.closest?.('#tutorialLayer,.tutorial-practice')))return;
      if(!frame)frame=requestAnimationFrame(()=>{frame=0;reposition();});
    });observer.observe(document.body,{subtree:true,childList:true});
    window.addEventListener('resize',()=>reposition());document.addEventListener('scroll',()=>{if(active&&!frame)frame=requestAnimationFrame(()=>{frame=0;reposition();});},true);
    document.addEventListener('keydown',e=>{if(active&&e.key==='Escape'){e.preventDefault();e.stopImmediatePropagation();finish();}},true);
  }
  globalThis.DashboardTutorial={ready,offer};
})();
