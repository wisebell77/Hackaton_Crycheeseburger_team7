"""선택적 연결 예제: 기존 엔진 추천안에 정책을 적용해 평가한다 (최적화 탐색 아님)."""
import argparse
import json
from pathlib import Path

from order_policy import compile_policy
from order_policy.engine_adapter import (load_engine, catalog_from_dataset, scope_dataset,
    engine_params, buffered_plan, evaluate_with_policy)


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--engine-dir",default=str(Path(__file__).resolve().parent),help="엔진 루트 (기본: model/)")
    ap.add_argument("--data",required=True)
    ap.add_argument("--owner",default="균형 있게 주문하자")
    ap.add_argument("--order",default="")
    ap.add_argument("--asof",default=None)
    ap.add_argument("--max-scenarios",type=int,default=100)
    ap.add_argument("--out",default="policy_out")
    a=ap.parse_args()
    if a.max_scenarios < 1:
        ap.error("--max-scenarios는 양의 정수")
    load_engine(a.engine_dir)
    from src.loader import load_all
    from src.engine import run_engine
    from src.engine.requirement import backtest_ingredient_arrays
    from src.engine.evaluate import historical_scenarios
    from src import prep
    full=load_all(a.data)
    p=compile_policy(a.owner,a.order,catalog=catalog_from_dataset(full))
    dest=Path(a.out);dest.mkdir(parents=True,exist_ok=True)
    (dest/"policy.json").write_text(json.dumps(p,ensure_ascii=False,indent=2),encoding="utf-8")
    if p["status"]!="ready":
        print(json.dumps(p["issues"],ensure_ascii=False));return 2
    ds=scope_dataset(full,p)
    out=run_engine(ds,engine_params(p,asof_date=a.asof))
    plan=buffered_plan(out,p)
    daily=prep.daily_sales(ds)
    bt=backtest_ingredient_arrays(ds,daily,out.params)
    sc=historical_scenarios(bt,out.params.horizon_days,max_scenarios=a.max_scenarios) if bt is not None else None
    report,ev=evaluate_with_policy(ds,out,plan,p,scenarios=sc,daily=daily)
    plan.to_csv(dest/"candidate_plan.csv",index=False,encoding="utf-8-sig")
    (dest/"assessment.json").write_text(json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False),encoding="utf-8")
    print(json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False))
    return 0 if report["feasible"] else 3


if __name__=="__main__":
    raise SystemExit(main())
