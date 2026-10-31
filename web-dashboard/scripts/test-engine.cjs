/* 브라우저 발주 엔진(dist/engine-core.js) ↔ Python 엔진(model/) 대조 + 후보 생성 규칙 검사.
   기준값: scripts/engine-fixture.json (scripts/export_engine_params.py 가 생성). 추가 npm 패키지 없음.
   실행: node web-dashboard/scripts/test-engine.cjs */
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
const root = require('node:path').resolve(__dirname, '..');
const json = f => JSON.parse(fs.readFileSync(root + '/' + f));
const data = json('dist/data.json'), team = json('dist/team-forecasts.json'), promo = json('dist/promotion-inputs.json');
const params = json('dist/engine-params.json'), fx = json('scripts/engine-fixture.json');
for (const f of ['model-engine.js', 'promotion-engine.js', 'engine-core.js']) vm.runInThisContext(fs.readFileSync(root + '/dist/' + f, 'utf8'));
OrderEngine.configure(params);

const close = (a, b, label, tol = 1e-7) => assert(Math.abs(a - b) <= tol * Math.max(1, Math.abs(a), Math.abs(b)), `${label}: JS ${a} ≠ Python ${b}`);
const asof = fx.asof, win = team.windows[asof];
FreshOrderEngine.configure({ forecast: ({ item, storeId }) => win.stores[storeId][item.ingredient_id] });

// 정규분포 역함수 (Python NormalDist().inv_cdf 값)
close(OrderEngine.zValue(0.95), 1.6448536269514722, 'z95', 1e-14);
close(OrderEngine.zValue(0.99), 2.3263478740408408, 'z99', 1e-14);
close(OrderEngine.zValue(0.90), 1.2815515655446004, 'z90', 1e-14);
close(OrderEngine.zValue(0.9999), 3.7190164854556804, 'z9999', 1e-13);

let nRec = 0, nEval = 0;
for (const store of Object.keys(fx.stores)) {
  const snap = data.snapshots[store + '_' + asof], before = JSON.stringify(snap);
  const items = FreshOrderEngine.buildItems(snap, { storeId: store, asOf: asof, suppliers: data.suppliers, overrides: {} });
  const proj = PromotionEngine.project(promo, asof, store, win.dates, []);
  const ctx = OrderEngine.context({ asOf: asof, store, dates: win.dates, suppliers: data.suppliers, menuForecast: proj.menu,
    baseIngredients: proj.ingredients, menus: promo.menus, bom: promo.bom });
  const ref = fx.stores[store];
  // (1) 발주 추천: 서비스 수준 90/95/99% 수량과 계산 항
  for (const [sl, lines] of Object.entries(ref.recommend)) for (const [iid, py] of Object.entries(lines)) {
    const i = items.find(x => x.ingredient_id === iid), js = OrderEngine.recommend(i, ctx, +sl);
    assert.equal(js.order_qty, py.order_qty, `${store} ${iid} ${sl} 발주수량`);
    assert.equal(js.window_days, py.window_days, `${store} ${iid} 커버일수`);
    for (const t of ['safety_stock', 'net_need_use', 'demand_window', 'unmet_before_arrival1', 'available', 'inbound', 'sigma_window', 'on_hand', 'expiring_excluded'])
      close(js[t], py[t], `${store} ${iid} ${sl} ${t}`);
    nRec++;
  }
  // (2) 발주안 평가: 95% 추천안과 발주 없음 (같은 239개 시나리오)
  for (const [name, py] of Object.entries(ref.evaluate)) {
    const ev = OrderEngine.evaluate(items, py.plan, ctx);
    for (const [k, v] of Object.entries(py.totals)) close(ev.totals[k], v, `${store} ${name} ${k}`);
    for (const [iid, l] of Object.entries(py.lines)) for (const [k, v] of Object.entries(l)) close(ev.lines[iid][k], v, `${store} ${name} ${iid} ${k}`);
    for (const [k, pz] of Object.entries(params.presets)) close(OrderEngine.score(ev, pz), py.scores[k], `${store} ${name} 점수 ${k}`);
    nEval++;
  }
  // (3) 후보 생성: 성향별 발주안이 최소 발주금액을 지키고, 변형 규칙이 맞는지
  const out = OrderEngine.plan(items, ctx);
  for (const [key, p] of Object.entries(out.presets)) {
    assert(p.chosen.feasible, `${store} ${key} 선택안이 최소금액 위반`);
    assert.equal(p.service_level, params.presets[key].service_level);
    for (const c of p.candidates) {
      if (c.variant === '최소금액 연기') for (const is of p.candidates[0].issues) for (const i of items) if (i.supplier_id === is.supplier_id) assert.equal(c.qty[i.ingredient_id], 0);
      if (c.variant === '최소금액 증량') assert.equal(OrderEngine.minOrderIssues(items, c.qty, ctx).length, 0, `${store} ${key} 증량안 최소금액`);
      for (const i of items) assert(Number.isInteger(c.qty[i.ingredient_id]) && c.qty[i.ingredient_id] >= 0);
    }
    const best = [...p.candidates].filter(c => c.feasible).sort((a, b) => a.score - b.score)[0];
    assert.equal(p.chosen.score, best.score);
  }
  assert(out.zero.totals.purchase_amount === 0);
  assert.equal(JSON.stringify(snap), before, '재고 스냅숏 불변');
}
// 행사 반영: 수요가 늘면 같은 서비스 수준의 추천량이 줄지 않는다
{
  const store = 'S02', snap = data.snapshots[store + '_' + asof];
  const items = FreshOrderEngine.buildItems(snap, { storeId: store, asOf: asof, suppliers: data.suppliers, overrides: {} });
  const plan = [{ promotion_id: 'T1', name: '테스트', store_ids: [store], target_menu_ids: ['M004', 'M005'], start_date: win.dates[0], end_date: win.dates[2],
    offer: { type: 'percent_off', value: 10 }, response: { mode: 'manual', uplift_pct: 50, reason: '테스트 가정' } }];
  const base = PromotionEngine.project(promo, asof, store, win.dates, []), up = PromotionEngine.project(promo, asof, store, win.dates, plan);
  const mk = proj => OrderEngine.context({ asOf: asof, store, dates: win.dates, suppliers: data.suppliers, menuForecast: proj.menu, baseIngredients: base.ingredients, menus: promo.menus, bom: promo.bom });
  const withF = (proj) => items.map(i => ({ ...i, forecast: proj.ingredients[i.ingredient_id] }));
  const r0 = withF(base).map(i => OrderEngine.recommend(i, mk(base), 0.95)), r1 = withF(up).map(i => OrderEngine.recommend(i, mk(up), 0.95));
  r0.forEach((a, k) => assert(r1[k].order_qty >= a.order_qty, `${a.ingredient_id} 행사 시 추천 감소`));
  assert(r1.some((b, k) => b.order_qty > r0[k].order_qty), '행사 반영이 추천에 영향 없음');
}
// 9/30 이후 기준일(점장 기록 날짜)에도 계산이 끝까지 돌고 값이 유한한지
let nLater = 0;
for (const later of ['2026-10-01', '2026-10-07', '2026-10-14']) {
  const w = team.windows[later];
  FreshOrderEngine.configure({ forecast: ({ item, storeId }) => w.stores[storeId][item.ingredient_id] });
  for (const store of ['S01', 'S02', 'S03']) {
    const items = FreshOrderEngine.buildItems(data.snapshots[store + '_' + asof], { storeId: store, asOf: later, suppliers: data.suppliers, overrides: {} });
    const proj = PromotionEngine.project(promo, later, store, w.dates, []);
    const ctx = OrderEngine.context({ asOf: later, store, dates: w.dates, suppliers: data.suppliers, menuForecast: proj.menu, baseIngredients: proj.ingredients, menus: promo.menus, bom: promo.bom });
    const out = OrderEngine.plan(items, ctx);
    for (const p of Object.values(out.presets)) {
      assert(p.chosen.feasible);
      for (const v of Object.values(p.chosen.eval.totals)) assert(Number.isFinite(v), `${later} ${store} 비유한 값`);
    }
    nLater++;
  }
}
console.log(`PASS: 추천 ${nRec}건(3개 서비스 수준)·평가 ${nEval}건이 Python 엔진과 일치, 후보·최소금액 규칙, 행사 반영, 스냅숏 불변, 이후 기준일 ${nLater}건`);
