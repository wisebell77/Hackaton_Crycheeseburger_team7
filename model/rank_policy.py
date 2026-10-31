"""후보 발주안 생성·평가·순위: 성향 격자 × 최소금액 변형 → 같은 시나리오 → 정책 점수.

사용:
    python rank_policy.py --data <CSV 폴더> --asof 2026-09-23 \
        --owner '안전하게 주문하자' --order 'S03만, 20만원 미만, 10% 여유'
산출(--out, 기본 policy_out/rank):
    ranking.csv        후보별 수치 표 (실행 가능 → 점수 → 금액 순)
    ranking.json       policy.rank_candidates 결과 (best, ranked, reports)
    best_plan.csv      최적 후보의 발주안 (없으면 생성 안 함)
    candidates/*.csv   후보별 발주안
    policy.json        적용 정책
종료 코드 0 = 최적 후보 있음, 2 = 자연어 해석 문제, 3 = 실행 가능한 후보 없음
"""
import argparse
import json
import sys
from pathlib import Path

from order_policy import compile_policy
from order_policy.engine_adapter import catalog_from_dataset, load_engine, scope_dataset


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine-dir", default=str(Path(__file__).resolve().parent), help="엔진 루트 (기본: model/)")
    ap.add_argument("--data", required=True)
    ap.add_argument("--owner", default="균형 있게 주문하자")
    ap.add_argument("--order", default="")
    ap.add_argument("--asof", default=None)
    ap.add_argument("--levels", default="0.90,0.95,0.99", help="격자 서비스 수준 (쉼표)")
    ap.add_argument("--max-scenarios", type=int, default=100)
    ap.add_argument("--out", default="policy_out/rank")
    a = ap.parse_args()
    load_engine(a.engine_dir)
    from src.loader import load_all
    from order_policy.candidates import generate_candidates

    full = load_all(a.data)
    policy = compile_policy(a.owner, a.order, catalog=catalog_from_dataset(full))
    dest = Path(a.out); dest.mkdir(parents=True, exist_ok=True)
    (dest / "policy.json").write_text(json.dumps(policy, ensure_ascii=False, indent=2), encoding="utf-8")
    if policy["status"] != "ready":
        print(json.dumps(policy["issues"], ensure_ascii=False)); return 2
    ds = scope_dataset(full, policy)
    levels = tuple(float(x) for x in a.levels.split(",") if x.strip())
    res = generate_candidates(ds, policy, asof_date=a.asof, levels=levels, max_scenarios=a.max_scenarios)
    res["table"].to_csv(dest / "ranking.csv", index=False, encoding="utf-8-sig")
    (dest / "ranking.json").write_text(json.dumps(res["ranking"], ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    (dest / "candidates").mkdir(exist_ok=True)
    for c in res["candidates"]:
        safe = "".join(ch if ch.isalnum() or ch in "._-·% " else "_" for ch in c["name"]).strip()
        c["plan"].to_csv(dest / "candidates" / f"{safe}.csv", index=False, encoding="utf-8-sig")
    import pandas as pd
    pd.set_option("display.width", 250); pd.set_option("display.max_columns", 30)
    show = res["table"].drop(columns=["별칭", "비고"])
    print(f"정책: mode={policy['mode']} 서비스={policy['preferences']['service_level']} 여유={policy['preferences']['extra_demand_buffer_ratio']} "
          f"예산={policy['constraints']['budget']} 매장={policy['constraints']['store_ids']} 제외={policy['constraints']['excluded_ingredient_ids']}")
    print(show.to_string(index=False))
    if res["best"]:
        best = next(c for c in res["candidates"] if c["name"] == res["best"])
        best["plan"].to_csv(dest / "best_plan.csv", index=False, encoding="utf-8-sig")
        print(f"\n최적 후보: {res['best']} → {dest / 'best_plan.csv'}")
        return 0
    print("\n실행 가능한 후보 없음 (예산·공급사 규칙을 자동 완화하지 않음)")
    return 3


if __name__ == "__main__":
    sys.exit(main())
