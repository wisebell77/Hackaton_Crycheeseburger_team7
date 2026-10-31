"""Run track 1 with structured promotions; no order submission or plan ranking."""
import argparse
import json
from pathlib import Path

from promotion_forecast.bootstrap import load_engine


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--data", required=True, help="원본 CSV 폴더; 파일명 (1) 접미사도 기존 로더가 처리")
    p.add_argument("--asof", required=True, help="예측 기준일 영업 종료, YYYY-MM-DD")
    p.add_argument("--horizon", type=int, default=14)
    p.add_argument("--promotions", required=True, help="계획 JSON; 무행사는 promotions: []")
    p.add_argument("--out", default="promotion_out")
    p.add_argument("--engine-dir", default=None)
    args = p.parse_args()
    if not 1 <= args.horizon <= 366:
        p.error("horizon은 1~366일")
    load_engine(Path(__file__).resolve().parent, args.engine_dir)
    import pandas as pd
    from src.loader import load_all
    from src import prep
    from src.engine.params import EngineParams
    from src.engine.requirement import ingredient_requirement
    from promotion_forecast import validate_plan, make_forecaster

    ds = load_all(args.data)
    params = EngineParams(asof_date=pd.Timestamp(args.asof), horizon_days=args.horizon)
    payload = json.loads(Path(args.promotions).read_text(encoding="utf-8-sig"))
    validated = validate_plan(payload, ds, params)
    notes = []
    forecast, basis = make_forecaster(validated)(ds, params, notes=notes)
    req = ingredient_requirement(forecast, ds)
    baseline = forecast.assign(forecast_qty=forecast.baseline_forecast_qty)
    baseline_req = ingredient_requirement(baseline, ds).rename(columns={"requirement_qty": "baseline_requirement_qty"})
    req = req.merge(baseline_req.drop(columns="use_unit"), on=["date", "store_id", "ingredient_id"], validate="one_to_one")
    req["promo_increment_qty"] = req.requirement_qty - req.baseline_requirement_qty
    # Validate and calculate first; do not leave a new partial result on failure.
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    forecast.to_csv(out / "forecast_menu_daily.csv", index=False, encoding="utf-8-sig")
    req.to_csv(out / "requirement_ingredient_daily.csv", index=False, encoding="utf-8-sig")
    # Nested evidence must survive CSV serialization as valid JSON.
    for col in basis:
        if basis[col].dtype == object:
            basis[col] = basis[col].map(lambda v: json.dumps(v, ensure_ascii=False, default=str) if isinstance(v, (list, dict)) else v)
    basis.to_csv(out / "forecast_basis.csv", index=False, encoding="utf-8-sig")
    (out / "validated_plan.json").write_text(json.dumps(validated, ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "assumptions.json").write_text(json.dumps({"asof": args.asof, "assumptions": notes, "loader_warnings": ds.warnings}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"예측 완료: {out.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
