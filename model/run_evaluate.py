"""발주안 평가 — 발주안(CSV)을 넣으면 그 발주안을 실행했을 때의 품절·폐기·비용을 기대 경로와 과거 잔차 시나리오 분포로 예측한다.

사용 예 (이 파일이 있는 폴더에서):
    # 기본: '발주 없음' 과 '엔진 추천안' 두 가지를 평가해 비교
    python run_evaluate.py --data "<CSV 폴더>"
    # 내 발주안 CSV (store_id, ingredient_id, order_qty[, order_date | arrival_date, order_type]) 를 평가
    python run_evaluate.py --data "<CSV 폴더>" --plan my_plan.csv --plan other_plan.csv
    # 추천안을 1.3배 한 '안전 추구' 안도 같이
    python run_evaluate.py --data "<CSV 폴더>" --scale 1.3 --scale 0.8

산출물 (--out, 기본 eval_out/):
    compare.csv                      발주안별 비교 (발주 금액, 기대 품절 손실매출, 기대 폐기 비용, 기대 총손실, p90, 품절 확률)
    <발주안>/lines.csv               매장×식자재 결과 (기대 경로 + 시나리오 분포)
    <발주안>/daily.csv               기대 경로 일별 전개 (수요, 기말재고, 부족, 폐기) + p90 부족·p10 재고
    <발주안>/scenarios.csv           시나리오별 전체 손실 (분포 그래프용)
    <발주안>/plan_normalized.csv     도착일·단가·금액을 붙인 발주안
    <발주안>/totals.json, assumptions.txt
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import pandas as pd  # noqa: E402

from src import prep  # noqa: E402
from src.engine import EngineParams, run_engine  # noqa: E402
from src.engine.evaluate import (compare_plans, evaluate_plan, historical_scenarios, plan_from_orders,  # noqa: E402
                                 scale_plan, zero_plan)
from src.engine.requirement import backtest_ingredient_arrays  # noqa: E402
from src.loader import load_all  # noqa: E402


def _safe(name: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "._-×" else "_" for ch in name)


def main() -> int:
    ap = argparse.ArgumentParser(description="발주안 평가 (품절·폐기·비용 예측)")
    ap.add_argument("--data", default="data", help="CSV 17개가 든 폴더 또는 zip (기본: 이 폴더 옆의 data/)")
    ap.add_argument("--out", default="eval_out")
    ap.add_argument("--plan", action="append", default=[], help="발주안 CSV (여러 번 가능). 열: store_id, ingredient_id, order_qty[, order_date, arrival_date, order_type]")
    ap.add_argument("--scale", action="append", type=float, default=[], help="엔진 추천안 × 배율 발주안 추가 (여러 번 가능)")
    ap.add_argument("--no-default", action="store_true", help="'발주 없음'·'엔진 추천안' 기본 발주안을 넣지 않음")
    ap.add_argument("--asof", default=None); ap.add_argument("--horizon", type=int, default=14)
    ap.add_argument("--service-level", type=float, default=0.95); ap.add_argument("--demand-pct", type=float, default=0.0)
    ap.add_argument("--delay-buffer", type=int, default=0)
    ap.add_argument("--max-scenarios", type=int, default=None, help="시나리오 수 상한 (기본: 가능한 과거 시작일 전부)")
    ap.add_argument("--window", default="cycle", help="평가 창: cycle(다음 발주분 도착 전날까지, 기본) | horizon(기간 전체) | 일수")
    args = ap.parse_args()
    window = args.window if args.window in ("cycle", "horizon") else int(args.window)
    data_path = Path(args.data)
    if not data_path.exists():
        print(f"데이터 경로가 없습니다: {data_path.resolve()}\n  → CSV 17개를 data/ 폴더에 넣거나 --data <폴더 또는 zip> 을 지정하세요")
        return 1

    out_dir = Path(args.out); out_dir.mkdir(parents=True, exist_ok=True)
    print(f"[1/4] 데이터 읽는 중: {args.data}")
    ds = load_all(args.data)
    params = EngineParams(asof_date=pd.Timestamp(args.asof) if args.asof else None, horizon_days=args.horizon,
                          service_level=args.service_level, demand_multiplier=1 + args.demand_pct / 100.0,
                          delay_buffer_days=args.delay_buffer)
    print("[2/4] 엔진 실행 (예측·소요량·추천안)")
    out = run_engine(ds, params)
    daily = prep.daily_sales(ds)
    H = out.requirement["date"].nunique()
    bt = backtest_ingredient_arrays(ds, daily, out.params)
    scn = historical_scenarios(bt, H, max_scenarios=args.max_scenarios) if bt is not None else None
    print(f"      기준일 {out.params.asof.date()} · 예측 {H}일 · 평가 창 {window} · 시나리오 {scn.n if scn else 0}개 (과거 {scn.start_dates[0].date() if scn and scn.start_dates else '-'}~ 시작 블록)")

    plans: dict[str, pd.DataFrame] = {}
    if not args.no_default:
        plans["발주 없음"] = zero_plan()
        plans["엔진 추천안"] = plan_from_orders(out.orders.lines)
    for f in args.scale:
        plans[f"추천안×{f:g}"] = scale_plan(plan_from_orders(out.orders.lines), f, note=f"추천×{f:g}")
    for pth in args.plan:
        p = Path(pth)
        plans[p.stem] = pd.read_csv(p, encoding="utf-8-sig")
    if not plans:
        print("평가할 발주안이 없습니다 (--plan 또는 --scale 지정, 또는 --no-default 제거)")
        return 1

    print(f"[3/4] 발주안 {len(plans)}개 평가")
    evals = []
    for name, plan in plans.items():
        ev = evaluate_plan(ds, out.params, out.forecast, out.requirement, plan, scenarios=scn, name=name,
                           lot_inputs=out.projection.lot_inputs, daily=daily, window=window)
        evals.append(ev)
        d = out_dir / _safe(name); d.mkdir(parents=True, exist_ok=True)
        ev.lines.to_csv(d / "lines.csv", index=False, encoding="utf-8-sig")
        ev.daily.to_csv(d / "daily.csv", index=False, encoding="utf-8-sig")
        ev.scenarios.to_csv(d / "scenarios.csv", index=False, encoding="utf-8-sig")
        ev.plan.to_csv(d / "plan_normalized.csv", index=False, encoding="utf-8-sig")
        (d / "totals.json").write_text(json.dumps(ev.totals, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        (d / "assumptions.txt").write_text("\n".join(f"- {a}" for a in ev.assumptions), encoding="utf-8")
        t = ev.totals
        print(f"      {name:>12}: 발주 {t['purchase_amount']:>12,.0f}원 | 기대 품절손실 {t['mean_lost_revenue']:>10,.0f}원 "
              f"| 기대 폐기 {t['mean_waste_value']:>9,.0f}원 | 기대 총손실 {t['mean_loss']:>10,.0f}원 (p90 {t['p90_loss']:,.0f}) "
              f"| 기대 품절 품목 {t['mean_stockout_pairs']:.1f}개 (기대경로 {t['exp_stockout_pairs_window']}개, 1건 이상 확률 {t['p_any_stockout']:.0%})")
    cmp = compare_plans(evals)
    cmp.to_csv(out_dir / "compare.csv", index=False, encoding="utf-8-sig")
    print("[4/4] 저장 완료:", out_dir.resolve())
    pd.set_option("display.width", 220); pd.set_option("display.max_columns", 20)
    print(cmp.to_string(index=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
