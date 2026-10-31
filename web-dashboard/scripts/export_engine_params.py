"""브라우저 발주 엔진(dist/engine-core.js)이 쓰는 파라미터와 Python 대조 기준값을 내보낸다. 원본 CSV 는 내보내지 않는다.

출력
  dist/engine-params.json        식자재별 σ(커버일수 1~7일 합계 백테스트 RMSE)·급변 표시, 과거 잔차 시나리오(7일 블록),
                                 성향 프리셋(model/order_policy PRESETS), 급변 σ 배율
  scripts/engine-fixture.json    model/ 의 Python 엔진으로 계산한 기준값 (9/30 기준, 매장별)
                                 - 서비스 수준 90/95/99% 발주 추천 (수량·안전재고·각 항)
                                 - 95% 추천안과 발주 없음의 시나리오 평가 (평균·p90·품절 확률 …)
                                 test-engine.cjs 가 브라우저 엔진 결과와 대조한다.

실행 (저장소 최상위):
  python web-dashboard/scripts/export_engine_params.py --data <비공개 CSV 폴더>
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]          # web-dashboard/
MODEL = ROOT.parent / "model"
sys.path.insert(0, str(MODEL))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from order_policy.policy import PRESETS, empirical_cvar  # noqa: E402
from src import prep  # noqa: E402
from src.engine.evaluate import ScenarioSet, evaluate_plan, historical_scenarios, plan_from_orders, zero_plan  # noqa: E402
from src.engine.order import recommend_orders, supplier_plans  # noqa: E402
from src.engine.params import EngineParams, resolve_params  # noqa: E402
from src.engine.projection import project_inventory  # noqa: E402
from src.engine.requirement import backtest_ingredient_arrays, ingredient_sigma_backtest  # noqa: E402
from src.loader import load_all  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--data", type=Path, required=True)
ap.add_argument("--asof", default="2026-09-30", help="판매 관측 마지막 날 (σ·시나리오 기준)")
ap.add_argument("--horizon", type=int, default=7, help="대시보드 예측 일수")
ap.add_argument("--decimals", type=int, default=3, help="시나리오 비율 반올림 자릿수")
a = ap.parse_args()

ds = load_all(a.data)
daily = prep.daily_sales(ds)
params, _ = resolve_params(ds, EngineParams(asof_date=pd.Timestamp(a.asof), horizon_days=a.horizon))
H = a.horizon

# ---------------------------------------------------------------- σ·시나리오
bt = backtest_ingredient_arrays(ds, daily, params)
sig = ingredient_sigma_backtest(ds, daily, params, bt=bt)
scn = historical_scenarios(bt, H)
ratios = np.round(scn.ratios, a.decimals)                           # [N, S, I, H]
scn_r = ScenarioSet(ratios, scn.stores, scn.ingredients, scn.start_dates, H)
N = scn_r.n

sigma = {}
for r in sig.itertuples(index=False):
    sigma.setdefault(r.store_id, {})[r.ingredient_id] = {
        "by_L": [float(x) for x in r.sigma_by_L], "shift": bool(r.shift_flag),
        "shift_ratio": None if r.shift_ratio is None or pd.isna(r.shift_ratio) else round(float(r.shift_ratio), 4)}
rat = {s: {i: [float(x) for x in ratios[:, si, ii, :].reshape(-1)] for ii, i in enumerate(scn_r.ingredients)}
       for si, s in enumerate(scn_r.stores)}
engine_params = {
    "engine_id": "team7-order-engine-v1",
    "sales_through": str(params.asof.date()),
    "backtest_from": str(bt.dates[bt.eval_start].date()),
    "horizon": H,
    "shift_sigma_mult": float(params.shift_sigma_mult),
    "shift_band": list(params.shift_band),
    "presets": PRESETS,
    "sigma": sigma,
    "scenarios": {"n": N, "h": H, "stores": scn_r.stores, "ingredients": scn_r.ingredients,
                  "start_dates": [str(d.date()) for d in scn_r.start_dates], "ratios": rat},
}
(ROOT / "dist" / "engine-params.json").write_text(
    json.dumps(engine_params, ensure_ascii=False, allow_nan=False, separators=(",", ":")), encoding="utf-8")

# ---------------------------------------------------------------- Python 기준값 (대시보드와 같은 입력)
team = json.loads((ROOT / "dist" / "team-forecasts.json").read_text(encoding="utf-8"))
promo = json.loads((ROOT / "dist" / "promotion-inputs.json").read_text(encoding="utf-8"))
win, pwin = team["windows"][a.asof], promo["windows"][a.asof]
dates = pd.to_datetime(win["dates"])
req = pd.DataFrame([{"date": d, "store_id": s, "ingredient_id": i, "requirement_qty": float(v[k])}
                    for s, ing in win["stores"].items() for i, v in ing.items() for k, d in enumerate(dates)])
units = dict(zip(ds["03_ingredients"]["ingredient_id"], ds["03_ingredients"]["use_unit"]))
req["use_unit"] = req["ingredient_id"].map(units)
fc_menu = pd.DataFrame([{"date": d, "store_id": s, "menu_id": m, "forecast_qty": float(v["qty"][k])}
                        for s, ms in pwin["stores"].items() for m, v in ms.items() for k, d in enumerate(dates)])

fixture = {"asof": a.asof, "n_scenarios": N, "stores": {}}
proj = project_inventory(ds, req, params, [])
plans = supplier_plans(ds, params, [])
TERMS = ["order_qty", "safety_stock", "net_need_use", "demand_window", "unmet_before_arrival1", "available",
         "inbound", "sigma_window", "window_days", "on_hand", "expiring_excluded"]
for sl in (0.90, 0.95, 0.99):
    p_sl = EngineParams(**{**params.__dict__, "service_level": sl})
    orders = recommend_orders(ds, req, proj, p_sl, sig, plans, [])
    for r in orders.lines.itertuples(index=False):
        d = fixture["stores"].setdefault(r.store_id, {"recommend": {}, "evaluate": {}})
        d["recommend"].setdefault(f"{sl:.2f}", {})[r.ingredient_id] = {t: (int(getattr(r, t)) if t in ("order_qty", "window_days")
                                                                          else float(getattr(r, t))) for t in TERMS}
    if sl == 0.95:
        rec = plan_from_orders(orders.lines)

for sid in sorted(fixture["stores"]):
    ds_s = ds  # 평가는 매장별 행만 쓰도록 요구량·예측을 거른다
    r_s, f_s = req[req.store_id == sid], fc_menu[fc_menu.store_id == sid]
    li = proj.lot_inputs[proj.lot_inputs.store_id == sid]
    for name, plan in (("recommended_095", rec[rec.store_id == sid]), ("zero", zero_plan())):
        ev = evaluate_plan(ds_s, params, f_s, r_s, plan, scenarios=scn_r, name=name, lot_inputs=li, daily=daily)
        t = ev.totals
        scores = {}
        for k, pz in PRESETS.items():
            L = (pz["shortage_weight"] * ev.scenarios["lost_revenue"] + pz["waste_weight"] * ev.scenarios["waste_value"]).tolist()
            scores[k] = (1 - pz["tail_weight"]) * float(np.mean(L)) + pz["tail_weight"] * empirical_cvar(L, pz["cvar_alpha"])
        lines = {r.ingredient_id: {"p_stockout_window": r.p_stockout_window, "shortage_window_mean": r.shortage_window_mean,
                                   "waste_mean": r.waste_mean, "exp_shortage_window": r.exp_shortage_window,
                                   "exp_waste_qty": r.exp_waste_qty, "lost_revenue_mean": r.lost_revenue_mean,
                                   "window_days": int(r.window_days)} for r in ev.lines.itertuples(index=False)}
        fixture["stores"][sid]["evaluate"][name] = {
            "plan": {r.ingredient_id: int(r.order_qty) for r in plan.itertuples(index=False)},
            "totals": {k: t[k] for k in ("purchase_amount", "mean_lost_revenue", "mean_waste_value", "mean_loss", "p90_loss",
                                          "p95_loss", "p_any_stockout", "mean_stockout_pairs", "exp_lost_revenue",
                                          "exp_waste_value", "exp_loss")},
            "scores": scores, "lines": lines}
(ROOT / "scripts" / "engine-fixture.json").write_text(json.dumps(fixture, ensure_ascii=False, allow_nan=False), encoding="utf-8")
size = (ROOT / "dist" / "engine-params.json").stat().st_size
print(f"engine-params.json {size / 1024:.0f} KB · σ {sum(len(v) for v in sigma.values())}개 · 시나리오 {N}개 × {H}일 · "
      f"fixture 매장 {len(fixture['stores'])}개")
