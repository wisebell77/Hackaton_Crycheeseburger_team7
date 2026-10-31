/* 팀 발주 엔진 — model/src/engine 의 order.py(발주 추천)·evaluate.py(발주안 평가)와
   model/order_policy 의 성향 프리셋·후보 생성(candidates.py)을 브라우저로 옮긴 것. DOM 의존 없음.
   수치는 scripts/test-engine.cjs 가 Python 엔진 결과(scripts/engine-fixture.json)와 대조한다.

   OrderEngine.configure(engineParams)                    engine-params.json (σ·시나리오·프리셋)
   OrderEngine.context({asOf, store, dates, suppliers, menuForecast, baseIngredients, menus, bom})
   OrderEngine.recommend(item, ctx, serviceLevel)       → 발주 수량과 계산 항 (order.py _line_for)
   OrderEngine.evaluate(items, qtyMap, ctx)             → 시나리오 평가 (evaluate.py evaluate_plan)
   OrderEngine.plan(items, ctx)                         → 성향별(균형·안전·절약) 발주안 + 최소금액 변형 + 점수
*/
(function () {
'use strict';
let P = null;
const FAR = 1e9, FAR_DATE = '2099-12-31', EPS = 1e-9;
const addDays = (ds, n) => { const d = new Date(ds + 'T00:00:00Z'); d.setUTCDate(d.getUTCDate() + n); return d.toISOString().slice(0, 10); };
const diffDays = (a, b) => Math.round((new Date(b + 'T00:00:00Z') - new Date(a + 'T00:00:00Z')) / 86400000);
const sum = a => { let s = 0; for (const v of a) s += v; return s; };
const mean = a => a.length ? sum(a) / a.length : NaN;
const round9 = x => Math.round(x * 1e9) / 1e9;
const clip = (x, lo, hi) => Math.min(Math.max(x, lo), hi);

/* 표준정규 역함수: Wichura AS241 (CPython statistics.NormalDist.inv_cdf 와 같은 식) */
function invNorm(p) {
  const q = p - 0.5;
  let r, num, den;
  if (Math.abs(q) <= 0.425) {
    r = 0.180625 - q * q;
    num = (((((((2509.0809287301226727 * r + 33430.575583588128105) * r + 67265.770927008700853) * r + 45921.953931549871457) * r
      + 13731.693765509461125) * r + 1971.5909503065514427) * r + 133.14166789178437745) * r + 3.387132872796366608) * q;
    den = (((((((5226.495278852545925 * r + 28729.085735721942674) * r + 39307.89580009271061) * r + 21213.794301586595867) * r
      + 5394.1960214247511077) * r + 687.1870074920579083) * r + 42.313330701600911252) * r + 1.0);
    return num / den;
  }
  r = q <= 0 ? p : 1 - p;
  r = Math.sqrt(-Math.log(r));
  if (r <= 5) {
    r -= 1.6;
    num = (((((((7.7454501427834140764e-4 * r + 0.0227238449892691845833) * r + 0.24178072517745061177) * r + 1.27045825245236838258) * r
      + 3.64784832476320460504) * r + 5.7694972214606914055) * r + 4.6303378461565452959) * r + 1.42343711074968357734);
    den = (((((((1.05075007164441684324e-9 * r + 5.475938084995344946e-4) * r + 0.0151986665636164571966) * r + 0.14810397642748007459) * r
      + 0.68976733498510000455) * r + 1.6763848301838038494) * r + 2.05319162663775882187) * r + 1.0);
  } else {
    r -= 5;
    num = (((((((2.01033439929228813265e-7 * r + 2.71155556874348757815e-5) * r + 0.0012426609473880784386) * r + 0.026532189526576123093) * r
      + 0.29656057182850489123) * r + 1.7848265399172913358) * r + 5.4637849111641143699) * r + 6.6579046435011037772);
    den = (((((((2.04426310338993978564e-15 * r + 1.4215117583164458887e-7) * r + 1.8463183175100546818e-5) * r + 7.868691311456132591e-4) * r
      + 0.0148753612908506148525) * r + 0.13692988092273580531) * r + 0.59983220655588793769) * r + 1.0);
  }
  const x = num / den;
  return q < 0 ? -x : x;
}
const zValue = sl => invNorm(clip(+sl, 0.5, 0.9999));

/* numpy.percentile 기본(linear)과 같은 보간 */
function percentile(values, p) {
  const a = Float64Array.from(values).sort(), n = a.length;
  if (!n) return NaN;
  const rank = p / 100 * (n - 1), lo = Math.floor(rank), hi = Math.min(lo + 1, n - 1);
  return a[lo] + (a[hi] - a[lo]) * (rank - lo);
}
/* order_policy.policy.empirical_cvar: 나쁜 쪽 (1−α) 비율의 평균, 경계 표본은 필요한 만큼만 */
function cvar(values, alpha) {
  const v = [...values].sort((a, b) => b - a), mass = v.length * (1 - alpha), full = Math.floor(mass), frac = mass - full;
  let s = 0; for (let k = 0; k < full; k++) s += v[k];
  if (frac > 0) s += v[full] * frac;
  return s / mass;
}

/* ---------------------------------------------------------------- 입력 정리 */
function configure(params) { P = params; return api; }
function context(c) {
  const supplierMap = Object.fromEntries((c.suppliers || []).map(s => [s.supplier_id, s]));
  const menuPrice = Object.fromEntries((c.menus || []).map(m => [m.menu_id, +m.virtual_price]));
  return { ...c, supplierMap, menuPrice, H: c.dates.length };
}
const conv = i => +i.use_qty_per_order_unit;
const costPerUse = i => +i.price / conv(i);
const reqAt = (i, h) => i.forecast[Math.min(h, i.forecast.length - 1)];   // 예측 7일 밖은 마지막 날 값 (대시보드 계산기와 같은 가정)

/* 기준일 재고 로트 + 미입고 발주 (projection.initial_lots / inbound_lots) */
function baseLots(i, ctx) {
  const lots = i.lots.filter(l => +l.qty > 0).map((l, k) => ({ id: 'S' + k, src: 'stock', arrival: l.received || ctx.asOf, lastUse: l.expiry || FAR_DATE, qty: +l.qty }));
  const next = addDays(ctx.asOf, 1);
  (i.pending || []).filter(p => +p.qty > 0).forEach((p, k) => {
    const arrival = p.date < next ? next : p.date;
    lots.push({ id: 'P' + k, src: 'inbound', arrival, lastUse: addDays(arrival, +i.shelf_life_days - 1), qty: +p.qty });
  });
  return lots;
}
function newLot(i, qtyUnits, arrival) {
  return { id: 'N', src: 'new', arrival, lastUse: addDays(arrival, +i.shelf_life_days - 1), qty: qtyUnits * conv(i) };
}

/* 한 경로 FEFO 전개 (projection.simulate_pair): 입고 → 기한 지난 로트 폐기 → 마지막 사용일 빠른 순 소진 */
function simulatePath(lots, demand, dates) {
  const st = lots.map(l => ({ ...l, qty0: l.qty, onHand: l.src === 'stock' || l.arrival < dates[0], consumed: 0, wasted: 0, wasteDate: null }));
  const rows = [];
  for (let h = 0; h < dates.length; h++) {
    const d = dates[h];
    let received = 0, waste = 0;
    for (const s of st) if (!s.onHand && s.arrival <= d) { s.onHand = true; received += s.qty; }
    for (const s of st) if (s.onHand && s.qty > 0 && s.lastUse < d) { waste += s.qty; s.wasted += s.qty; s.wasteDate = d; s.qty = 0; }
    const need = demand[h] || 0;
    let left = need;
    const order = st.filter(s => s.onHand && s.qty > 0).sort((a, b) => a.lastUse.localeCompare(b.lastUse) || a.arrival.localeCompare(b.arrival));
    for (const s of order) { if (left <= 1e-12) break; const take = Math.min(s.qty, left); s.qty -= take; s.consumed += take; left -= take; }
    rows.push({ date: d, need, received, waste, consumed: need - left, shortage: Math.max(left, 0), end: st.reduce((a, s) => a + (s.onHand ? s.qty : 0), 0) });
  }
  return { rows, lots: st };
}

/* 식자재 σ (order._sigma_lookup): 커버일수 L 합계의 백테스트 RMSE, 7일 넘으면 √ 외삽, 급변 ×1.6, 행사 반영 시 수요 비율 */
function sigmaFor(i, ctx, L, scale = 1) {
  const r = P?.sigma?.[ctx.store]?.[i.ingredient_id];
  if (!r) return { sigma: 0, daily: 0, note: 'σ 정보 없음 → 0', shift: false, shiftRatio: null };
  const n = r.by_L.length, base = L <= n ? r.by_L[L - 1] : r.by_L[n - 1] * Math.sqrt(L / n);
  const mult = r.shift ? P.shift_sigma_mult : 1;
  let note = `과거 예측 오차(${L}일 합계 RMSE)`;
  if (r.shift) note += ` × ${mult} 급변 중(최근 2주/8주 ${r.shift_ratio})`;
  if (Math.abs(scale - 1) > 1e-9) note += ` × ${scale.toFixed(2)} 행사 반영 수요 비율`;
  return { sigma: base * mult * scale, daily: r.by_L[0], note, shift: r.shift, shiftRatio: r.shift_ratio };
}

/* ---------------------------------------------------------------- (1) 발주 추천: order._line_for */
function recommend(i, ctx, serviceLevel) {
  const a1 = i.sc.arrival, a2 = i.sc.nextArrival, wStart = addDays(ctx.asOf, 1);
  const span = Math.max(ctx.H, diffDays(ctx.asOf, a2));
  const dates = Array.from({ length: span }, (_, h) => addDays(ctx.asOf, h + 1));
  const demandPath = dates.map((_, h) => reqAt(i, h));
  const sim = simulatePath(baseLots(i, ctx), demandPath, dates);
  const windowDays = Math.max(diffDays(wStart, a2), 1);
  let demand = 0, unmet = 0, needBeforeA1 = 0;
  const daily = [];
  sim.rows.forEach((r, h) => {
    if (r.date >= wStart && r.date < a2) { demand += demandPath[h]; daily.push([r.date, demandPath[h]]); }
    if (r.date >= wStart && r.date < a1) { unmet += r.shortage; needBeforeA1 += demandPath[h]; }
  });
  const init = sim.lots.filter(l => l.src === 'stock');
  const onHand = sum(init.map(l => l.qty0));
  const expiring = init.filter(l => l.wasteDate && l.wasteDate < a2);
  const expiringExcluded = sum(expiring.map(l => l.wasted));
  const inb = sim.lots.filter(l => l.src === 'inbound' && l.arrival < a2);
  const inbound = sum(inb.map(l => l.qty0 - (l.wasteDate && l.wasteDate < a2 ? l.wasted : 0)));
  let scale = 1;
  if (ctx.baseIngredients) {
    const base = ctx.baseIngredients[i.ingredient_id] || [];
    const b = sum(dates.slice(0, Math.min(windowDays, base.length)).map((_, h) => base[h] || 0));
    const adj = sum(dates.slice(0, Math.min(windowDays, base.length)).map((_, h) => reqAt(i, h)));
    scale = b > 1e-9 ? clip(adj / b, 0.5, 3) : 1;
  }
  const sg = sigmaFor(i, ctx, windowDays, scale), z = zValue(serviceLevel);
  const safety = z * sg.sigma, available = onHand - expiringExcluded;
  const net = Math.max(0, demand - unmet + safety - available - inbound);
  const raw = Math.ceil(round9(net / conv(i)));
  const qty = net > 0 ? Math.max(raw, Math.ceil(+i.min_order_qty || 0)) : 0;
  return {
    ingredient_id: i.ingredient_id, service_level: +serviceLevel, z, order_qty: qty, raw_units: net / conv(i), ceil_units: raw,
    order_date: i.sc.order, arrival1: a1, arrival2: a2, window_start: wStart, window_end: addDays(a2, -1), window_days: windowDays,
    daily_requirement: daily, demand_window: demand, unmet_before_arrival1: unmet, need_before_arrival1: needBeforeA1,
    sigma_window: sg.sigma, sigma_daily: sg.daily, sigma_note: sg.note, shift: sg.shift, sigma_scale: scale,
    safety_stock: safety, on_hand: onHand, n_lots: init.length, expiring_excluded: expiringExcluded,
    expiring_lots: expiring.map(l => ({ lastUse: l.lastUse, wasted: l.wasted })), available, inbound,
    inbound_lines: inb.map(l => ({ date: l.arrival, qty: l.qty0 })), net_need_use: net,
    min_order_qty: +i.min_order_qty, use_qty_per_order_unit: conv(i), unit_price: +i.price, amount: qty * +i.price,
  };
}

/* ---------------------------------------------------------------- (2) 발주안 평가: evaluate.evaluate_plan */
function scenarioRatios(ctx, iid) {
  const sc = P?.scenarios, flat = sc?.ratios?.[ctx.store]?.[iid];
  return flat ? { n: sc.n, h: sc.h, flat } : null;
}
function expireIndex(lastUse, dates) {               // 마지막 사용일 다음 날 아침에 폐기 (기간 밖이면 FAR)
  if (!(lastUse < dates[dates.length - 1])) return FAR;
  let k = 0; while (k < dates.length && dates[k] <= lastUse) k++;
  return k;
}
function simulateScenarios(lots, demand, R, H) {       // evaluate._simulate_lots (경로 R개 벡터화)
  const L = lots.length, qty = new Float64Array(R * L);
  for (let r = 0; r < R; r++) for (let l = 0; l < L; l++) qty[r * L + l] = lots[l].qty;
  const shortage = new Float64Array(R * H), wEx = new Float64Array(R * H), wNew = new Float64Array(R * H), end = new Float64Array(R * H);
  for (let h = 0; h < H; h++) for (let r = 0; r < R; r++) {
    const o = r * L, k = r * H + h;
    for (let l = 0; l < L; l++) if (lots[l].arr <= h && lots[l].exp <= h) {
      if (lots[l].isNew) wNew[k] += qty[o + l]; else wEx[k] += qty[o + l];
      qty[o + l] = 0;
    }
    let rem = demand[k];
    for (let l = 0; l < L; l++) {
      if (lots[l].arr > h || lots[l].exp <= h) continue;
      const take = Math.min(qty[o + l], rem); qty[o + l] -= take; rem -= take;
    }
    shortage[k] = rem;
    let e = 0; for (let l = 0; l < L; l++) if (lots[l].arr <= h) e += qty[o + l];
    end[k] = e;
  }
  return { shortage, wEx, wNew, end };
}
function windowEnd(i, dates) {                          // evaluate._pair_windows('cycle'): 다음 발주분 도착 전날
  const e = addDays(i.sc.nextArrival, -1);
  let k = 0; while (k < dates.length && dates[k] <= e) k++;
  return clip(k - 1, 0, dates.length - 1);
}

function evaluate(items, qtyMap, ctx) {
  const dates = ctx.dates, H = dates.length, asOf = ctx.asOf;
  const didx = Object.fromEntries(dates.map((d, k) => [d, k]));
  const first = items.map(i => scenarioRatios(ctx, i.ingredient_id)).find(Boolean);
  const N = first ? first.n : 0, R = N + 1;
  const iids = items.map(i => i.ingredient_id), pos = Object.fromEntries(iids.map((id, k) => [id, k]));
  const fracs = [], per = [];
  let purchase = 0, nLines = 0;
  for (const i of items) {
    const q = Math.max(0, Math.round(+qtyMap[i.ingredient_id] || 0));
    if (q > 0) { purchase += q * +i.price; nLines++; }
    const sr = scenarioRatios(ctx, i.ingredient_id);
    const demand = new Float64Array(R * H);
    for (let h = 0; h < H; h++) {
      const base = reqAt(i, h); demand[h] = base;
      for (let n = 0; n < N; n++) demand[(n + 1) * H + h] = base * (sr && h < sr.h ? sr.flat[n * sr.h + h] : 1);
    }
    const lots = baseLots(i, ctx).map((l, k) => ({ ...l, ord: k }));
    if (q > 0) lots.push({ ...newLot(i, q, i.sc.arrival), ord: lots.length });
    const vl = lots.map(l => ({
      qty: l.qty, isNew: l.src === 'new', ord: l.ord,
      arr: (l.src === 'stock' || l.arrival <= asOf) ? 0 : (didx[l.arrival] ?? FAR),
      exp: expireIndex(l.lastUse, dates),
    })).sort((a, b) => a.exp - b.exp || a.arr - b.arr || a.ord - b.ord);
    const sim = simulateScenarios(vl, demand, R, H);
    const wEnd = windowEnd(i, dates);
    const frac = new Float64Array(R * H);
    for (let r = 0; r < R; r++) for (let h = 0; h <= wEnd; h++) {
      const k = r * H + h, d = demand[k];
      frac[k] = d > EPS ? clip(sim.shortage[k] / d, 0, 1) : 0;
    }
    fracs.push(frac);
    per.push({ i, q, sim, demand, wEnd });
  }
  /* 메뉴 단위 손실 매출: 메뉴 재료 중 가장 모자란 재료의 부족 비율 × 예측 판매 × 가격, 식자재 귀속은 가장 모자란 재료에 */
  const lost = new Float64Array(R), attr = items.map(() => new Float64Array(R));
  const bomBy = {};
  for (const b of ctx.bom) if (pos[b.ingredient_id] !== undefined && +b.qty_per_menu > 0) (bomBy[b.menu_id] ||= []).push(pos[b.ingredient_id]);
  for (const [mid, fc] of Object.entries(ctx.menuForecast || {})) {
    const idx = bomBy[mid]; if (!idx) continue;
    const price = ctx.menuPrice[mid] || 0;
    for (let r = 0; r < R; r++) for (let h = 0; h < H; h++) {
      let fm = -1, am = 0;
      for (let j = 0; j < idx.length; j++) { const f = fracs[idx[j]][r * H + h]; if (f > fm) { fm = f; am = j; } }
      const v = price * (fc[h] || 0) * fm;
      lost[r] += v; attr[idx[am]][r] += v;
    }
  }
  const lines = {}, wasteVal = new Float64Array(R), anyShort = new Uint8Array(R), nShort = new Float64Array(R);
  per.forEach(({ i, q, sim, demand, wEnd }, k) => {
    const c = costPerUse(i), sw = new Float64Array(R), sh = new Float64Array(R), wt = new Float64Array(R), wex = new Float64Array(R), wnew = new Float64Array(R);
    for (let r = 0; r < R; r++) {
      for (let h = 0; h < H; h++) {
        const x = r * H + h; sh[r] += sim.shortage[x]; wex[r] += sim.wEx[x]; wnew[r] += sim.wNew[x];
        if (h <= wEnd) sw[r] += sim.shortage[x];
      }
      wt[r] = wex[r] + wnew[r];
      wasteVal[r] += wt[r] * c;
      if (sw[r] > EPS) { anyShort[r] = 1; nShort[r] += 1; }
    }
    const S = a => Array.from(a.slice(1)), loss = Array.from(attr[k]).map((v, r) => v + wt[r] * c);
    lines[i.ingredient_id] = {
      order_qty: q, amount: q * +i.price, window_end: dates[wEnd], window_days: wEnd + 1,
      demand_window: sum(Array.from(demand.slice(0, wEnd + 1))),
      exp_shortage_window: sw[0], exp_shortage_horizon: sh[0], exp_waste_exist_qty: wex[0], exp_waste_new_qty: wnew[0],
      exp_waste_qty: wt[0], exp_waste_value: wt[0] * c, exp_lost_revenue: attr[k][0], exp_end_on_hand: sim.end[H - 1],
      p_stockout_window: N ? mean(S(sw).map(v => v > EPS ? 1 : 0)) : (sw[0] > EPS ? 1 : 0),
      shortage_window_mean: N ? mean(S(sw)) : sw[0], shortage_window_p90: N ? percentile(S(sw), 90) : sw[0],
      waste_mean: N ? mean(S(wt)) : wt[0], waste_p90: N ? percentile(S(wt), 90) : wt[0],
      lost_revenue_mean: N ? mean(S(attr[k])) : attr[k][0], loss_mean: N ? mean(loss.slice(1)) : loss[0],
      end_p10: N ? percentile(Array.from({ length: N }, (_, n) => sim.end[(n + 1) * H + H - 1]), 10) : sim.end[H - 1],
    };
  });
  const totLoss = lost.map((v, r) => v + wasteVal[r]);
  const scen = n => Array.from(n.slice(1));
  const totals = {
    n_scenarios: N, horizon_days: H, purchase_amount: purchase, n_order_lines: nLines,
    exp_lost_revenue: lost[0], exp_waste_value: wasteVal[0], exp_loss: totLoss[0],
    mean_lost_revenue: N ? mean(scen(lost)) : lost[0], mean_waste_value: N ? mean(scen(wasteVal)) : wasteVal[0],
    mean_loss: N ? mean(scen(totLoss)) : totLoss[0], p90_loss: N ? percentile(scen(totLoss), 90) : totLoss[0],
    p95_loss: N ? percentile(scen(totLoss), 95) : totLoss[0],
    p_any_stockout: N ? mean(scen(anyShort)) : anyShort[0], mean_stockout_pairs: N ? mean(scen(nShort)) : nShort[0],
  };
  return { totals, lines, scenarios: { lost: scen(lost), waste: scen(wasteVal) } };
}

/* ---------------------------------------------------------------- (3) 성향·후보: order_policy PRESETS + candidates.py */
function score(ev, preset) {
  const L = ev.scenarios.lost.map((v, n) => preset.shortage_weight * v + preset.waste_weight * ev.scenarios.waste[n]);
  if (!L.length) return preset.shortage_weight * ev.totals.exp_lost_revenue + preset.waste_weight * ev.totals.exp_waste_value;
  return (1 - preset.tail_weight) * mean(L) + preset.tail_weight * cvar(L, preset.cvar_alpha);
}
function minOrderIssues(items, qtyMap, ctx) {
  const by = {};
  for (const i of items) { const q = +qtyMap[i.ingredient_id] || 0; if (q > 0) by[i.supplier_id] = (by[i.supplier_id] || 0) + q * +i.price; }
  return Object.entries(by).map(([sid, amount]) => ({ supplier_id: sid, amount, minimum: +(ctx.supplierMap[sid]?.min_order_amount || 0) }))
    .filter(x => x.amount > 0 && x.amount < x.minimum);
}
function topUp(items, qtyMap, issue) {                 // 소진량(포장단위) 큰 품목부터 1단위씩 돌아가며 최소금액까지 (명세 7절 규칙)
  const q = { ...qtyMap };
  const cand = items.filter(i => i.supplier_id === issue.supplier_id && (q[i.ingredient_id] || 0) > 0 && +i.price > 0)
    .map(i => ({ i, daily: mean(i.forecast) / conv(i) }))
    .sort((a, b) => b.daily - a.daily || +b.i.price - +a.i.price);
  if (!cand.length) return null;
  let total = issue.amount, step = 0;
  while (total < issue.minimum && step < 10000) { const c = cand[step % cand.length]; q[c.i.ingredient_id] += 1; total += +c.i.price; step++; }
  return q;
}
const planKey = q => Object.entries(q).filter(([, v]) => v > 0).sort().map(([k, v]) => k + ':' + v).join('|');

function plan(items, ctx) {
  const presets = P.presets, cache = new Map(), recs = {};
  const evalCached = q => { const k = planKey(q); if (!cache.has(k)) cache.set(k, evaluate(items, q, ctx)); return cache.get(k); };
  const zero = evalCached({});
  const out = { presets: {}, zero };
  for (const [key, pz] of Object.entries(presets)) {
    const sl = pz.service_level;
    recs[sl] ||= Object.fromEntries(items.map(i => [i.ingredient_id, recommend(i, ctx, sl)]));
    const base = Object.fromEntries(items.map(i => [i.ingredient_id, recs[sl][i.ingredient_id].order_qty]));
    const cands = [{ variant: '기본', qty: base, note: '' }];
    const issues = minOrderIssues(items, base, ctx);
    if (issues.length) {
      const post = { ...base };
      let up = { ...base }, upOk = true;
      for (const is of issues) {
        for (const i of items) if (i.supplier_id === is.supplier_id) post[i.ingredient_id] = 0;
        const t = topUp(items, up, is); if (t) up = t; else upOk = false;
      }
      const desc = issues.map(x => `${ctx.supplierMap[x.supplier_id]?.supplier_name || x.supplier_id} ${Math.round(x.amount).toLocaleString('ko-KR')}원 < 최소 ${x.minimum.toLocaleString('ko-KR')}원`).join(', ');
      cands.push({ variant: '최소금액 연기', qty: post, note: `${desc} → 이번엔 발주하지 않고 다음 발주일로` });
      if (upOk) cands.push({ variant: '최소금액 증량', qty: up, note: `${desc} → 소진 빠른 품목부터 1단위씩 채움` });
    }
    for (const c of cands) {
      c.issues = minOrderIssues(items, c.qty, ctx);
      c.feasible = c.issues.length === 0;
      c.eval = evalCached(c.qty);
      c.score = score(c.eval, pz);
    }
    const feasible = cands.filter(c => c.feasible).sort((a, b) => a.score - b.score || a.eval.totals.purchase_amount - b.eval.totals.purchase_amount);
    const best = feasible[0] || cands[0];
    out.presets[key] = { key, preset: pz, service_level: sl, chosen: best, candidates: cands, recommend: recs[sl] };
  }
  return out;
}

const api = { configure, context, recommend, evaluate, plan, score, minOrderIssues, zValue, percentile, cvar, get params() { return P; } };
globalThis.OrderEngine = api;
})();
