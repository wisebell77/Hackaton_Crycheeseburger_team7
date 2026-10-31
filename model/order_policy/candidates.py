"""후보 발주안 생성 → 같은 시나리오로 평가 → 정책 점수로 순위 (계획 (3)의 '후보 탐색' 단계).

후보 = 성향 격자 × 최소 발주금액 변형
- 성향 격자: 서비스 수준 {0.90, 0.95, 0.99} ∪ {정책값}  ×  여유율 {0, 정책값}.
  서비스 수준·여유율은 후보 수량을 바꾸는 값이고, 품절/폐기/꼬리 가중치는 정책 그대로 점수에만 쓴다.
  그래서 '안전 성향 + 예산 20만원' 처럼 선호와 예산이 부딪히면 예산 안에서 가장 점수가 좋은 후보가 뽑힌다.
- 최소 발주금액 변형: 매장×공급사×발주일 합계가 0 보다 크고 최소금액보다 작은 발주서가 있으면
    '연기' = 그 발주서 라인을 이번엔 0 으로 (다음 발주일로 미룸. 품절 위험은 평가로 드러난다)
    '증량' = 선정된 품목 중 하루 소진량(포장단위)이 큰 순으로 1단위씩 돌아가며 추가해 최소금액을 채운다
             (제공 데이터를 만든 합성 발주 정책, 명세서 7절과 같은 규칙. 제외 품목은 증량하지 않는다)
- '발주 없음' 을 기준선으로 함께 평가한다. 긴급발주 후보는 만들지 않는다.

점수·제약은 policy.rank_candidates 그대로: 운영 제약 위반(최소금액 미달 등)·예산·확률·폐기비 상한을 넘는 후보는 불가,
나머지를 (1−꼬리비중)×평균 + 꼬리비중×CVaR 로 비교, 동점이면 발주 금액이 낮은 안.

사용:
    res = generate_candidates(ds, policy, asof_date="2026-09-23")
    res["table"]          # 후보별 수치 표 (feasible, violations, purchase, p_any_stockout, score ...)
    res["best"]           # 최적 후보 이름 (없으면 None)
    res["candidates"]     # [{name, service_level, buffer, variant, plan, ev, candidate, violations, aliases}]
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any, Optional

import numpy as np
import pandas as pd

from .engine_adapter import buffered_plan, candidate_from_evaluation, check_operational
from .policy import rank_candidates

DEFAULT_LEVELS = (0.90, 0.95, 0.99)
PLAN_COLS = ["store_id", "ingredient_id", "order_qty", "order_date", "arrival_date", "order_type", "note"]


def _engine():
    """load_engine 뒤에만 import 가능한 엔진 모듈 (engine_adapter 와 같은 방식)."""
    from src import prep
    from src.engine import run_engine
    from src.engine.evaluate import evaluate_plan, historical_scenarios, zero_plan
    from src.engine.params import EngineParams
    from src.engine.requirement import backtest_ingredient_arrays
    return run_engine, EngineParams, evaluate_plan, historical_scenarios, zero_plan, backtest_ingredient_arrays, prep


def service_levels(policy: dict, levels=DEFAULT_LEVELS) -> list[float]:
    """격자 서비스 수준 + 정책의 서비스 수준 (중복 제거, 오름차순)."""
    return sorted({round(float(x), 4) for x in (*levels, policy["preferences"]["service_level"])})


def buffer_ratios(policy: dict) -> list[float]:
    """여유율 {0, 정책값}."""
    return sorted({0.0, round(float(policy["preferences"]["extra_demand_buffer_ratio"]), 4)})


def _with_buffer(policy: dict, ratio: float) -> dict:
    p = deepcopy(policy)
    p["preferences"]["extra_demand_buffer_ratio"] = float(ratio)
    return p


def plan_economics(out, plan: pd.DataFrame) -> pd.DataFrame:
    """발주안 라인에 공급사·단가·발주단위당 사용량·하루 소진량(포장단위)·금액을 붙인다 (엔진 추천 라인의 값 사용)."""
    cols = ["store_id", "ingredient_id", "supplier_id", "unit_price", "use_qty_per_order_unit", "avg_daily_req", "min_order_qty"]
    lines = out.orders.lines[cols].drop_duplicates(["store_id", "ingredient_id"])
    p = plan.copy()
    p["order_qty"] = pd.to_numeric(p["order_qty"], errors="coerce").fillna(0).astype(int)
    p = p.merge(lines, on=["store_id", "ingredient_id"], how="left", validate="many_to_one")
    conv = p["use_qty_per_order_unit"].astype(float).replace(0, np.nan)
    p["daily_units"] = p["avg_daily_req"].astype(float).fillna(0.0) / conv
    p["amount"] = p["order_qty"] * p["unit_price"].astype(float)
    p["order_date"] = pd.to_datetime(p["order_date"])
    return p


def min_order_amounts(ds) -> dict[str, float]:
    """공급사별 1회 최소 발주금액 (05_suppliers, 0 = 제한 없음)."""
    from src import prep
    cal = prep.SupplierCalendar.from_dataset(ds)
    return {sid: float(cal.terms(sid).min_order_amount or 0.0) for sid in cal.supplier_ids}


def top_up(group: pd.DataFrame, minimum: float, excluded: set[str] | None = None,
           max_steps: int = 10000) -> tuple[pd.Series, float]:
    """한 발주서(매장×공급사×발주일)의 라인을 최소금액까지 증량한다.

    group : plan_economics 행들 (order_qty, unit_price, daily_units, ingredient_id)
    규칙  : 이미 선정된(수량 > 0) 품목 중 제외 품목이 아니고 단가가 있는 것을, 하루 소진량(포장단위) 큰 순으로
            1단위씩 돌아가며 추가 (명세서 7절). 반환 (새 order_qty Series, 합계 금액).
    증량 대상이 없으면 수량을 바꾸지 않는다.
    """
    excluded = excluded or set()
    qty = group["order_qty"].astype(int).copy()
    price = group["unit_price"].astype(float)
    total = float((qty * price).fillna(0).sum())
    cand = group[(qty > 0) & (~group["ingredient_id"].isin(excluded)) & price.notna() & (price > 0)]
    if cand.empty or total >= minimum:
        return qty, total
    order = list(cand.sort_values(["daily_units", "unit_price"], ascending=[False, False]).index)
    step = 0
    while total < minimum and step < max_steps:
        idx = order[step % len(order)]
        qty[idx] += 1
        total += float(price[idx])
        step += 1
    return qty, total


def min_order_variants(ds, out, plan: pd.DataFrame, policy: dict) -> list[tuple[str, pd.DataFrame, str]]:
    """최소금액 미달 발주서가 있으면 ('최소금액 연기', plan, 설명), ('최소금액 증량', plan, 설명). 없으면 []."""
    if plan is None or len(plan) == 0:
        return []
    p = plan_economics(out, plan)
    mins = min_order_amounts(ds)
    key = ["store_id", "supplier_id", "order_date"]
    totals = p.groupby(key)["amount"].sum()
    below = [k for k, v in totals.items() if pd.notna(v) and 0 < v < mins.get(k[1], 0.0)]
    if not below:
        return []
    excluded = set(policy["constraints"]["excluded_ingredient_ids"])
    postpone, topup = p.copy(), p.copy()
    for k in below:
        mask = (p["store_id"] == k[0]) & (p["supplier_id"] == k[1]) & (p["order_date"] == k[2])
        postpone.loc[mask, "order_qty"] = 0
        new_qty, _ = top_up(p[mask], mins[k[1]], excluded)
        topup.loc[mask, "order_qty"] = new_qty

    def finish(df: pd.DataFrame, note: str) -> pd.DataFrame:
        d = df[PLAN_COLS].copy()
        d["note"] = note
        return d[d["order_qty"] > 0].reset_index(drop=True)

    desc = "; ".join(f"{k[0]}/{k[1]}/{pd.Timestamp(k[2]).date()} {totals[k]:,.0f}원 < 최소 {mins[k[1]]:,.0f}원" for k in below)
    return [("최소금액 연기", finish(postpone, "최소금액 미달 발주서를 다음 발주일로 연기"), desc),
            ("최소금액 증량", finish(topup, "최소금액까지 소진량 큰 품목부터 1단위씩 증량"), desc)]


def _plan_key(plan: pd.DataFrame) -> tuple:
    rows = []
    for r in plan.itertuples(index=False):
        if int(r.order_qty) <= 0:
            continue
        arr = str(pd.Timestamp(r.arrival_date).date()) if pd.notna(r.arrival_date) else ""
        rows.append((r.store_id, r.ingredient_id, int(r.order_qty), arr, str(r.order_type or "정기")))
    return tuple(sorted(rows))


def generate_candidates(ds, policy: dict, asof_date=None, *, levels=DEFAULT_LEVELS, horizon_days: int = 14,
                        scenarios=None, daily: Optional[pd.DataFrame] = None, max_scenarios: Optional[int] = None,
                        include_zero: bool = True) -> dict[str, Any]:
    """격자 × 변형 후보를 만들고 같은 시나리오로 평가해 정책 점수로 순위를 매긴다.

    ds 는 scope_dataset 을 거친 데이터, policy 는 compile_policy 결과(status == 'ready').
    반환: candidates(list), ranking(policy.rank_candidates 결과), table(DataFrame), best(이름 또는 None), scenarios
    """
    if policy.get("status") != "ready":
        raise ValueError("미해결 자연어 조건이 있어 후보를 만들지 않음")
    run_engine, EngineParams, evaluate_plan, historical_scenarios, zero_plan, backtest_arrays, prep = _engine()
    daily = prep.daily_sales(ds) if daily is None else daily
    cands: list[dict[str, Any]] = []
    outs: dict[float, Any] = {}
    for sl in service_levels(policy, levels):
        params = EngineParams(asof_date=asof_date, service_level=sl, horizon_days=horizon_days, **policy["scenario"])
        out = run_engine(ds, params, daily=daily)
        outs[sl] = out
        for b in buffer_ratios(policy):
            plan = buffered_plan(out, _with_buffer(policy, b))
            base = f"서비스 {sl:.0%} · 여유 {b:.0%}"
            cands.append(dict(name=base, service_level=sl, buffer=b, variant="기본", plan=plan, out=out, note=""))
            for vname, vplan, desc in min_order_variants(ds, out, plan, policy):
                cands.append(dict(name=f"{base} · {vname}", service_level=sl, buffer=b, variant=vname, plan=vplan,
                                  out=out, note=desc))
    ref_out = outs[max(outs)]
    if include_zero:
        cands.append(dict(name="발주 없음", service_level=None, buffer=None, variant="기준선", plan=zero_plan(),
                          out=ref_out, note="비교 기준선"))
    # 같은 발주안은 한 번만 평가 (이름은 별칭으로 보존)
    seen: dict[tuple, dict[str, Any]] = {}
    uniq: list[dict[str, Any]] = []
    for c in cands:
        k = _plan_key(c["plan"])
        if k in seen:
            seen[k]["aliases"].append(c["name"])
            continue
        c["aliases"] = []
        seen[k] = c
        uniq.append(c)
    if scenarios is None:
        bt = backtest_arrays(ds, daily, ref_out.params)
        scenarios = historical_scenarios(bt, ref_out.params.horizon_days, max_scenarios=max_scenarios) if bt is not None else None
    for c in uniq:
        out = c["out"]
        _, violations = check_operational(ds, policy, c["plan"], out.params)
        violations = violations or []
        ev = evaluate_plan(ds, out.params, out.forecast, out.requirement, c["plan"], scenarios=scenarios, name=c["name"],
                           lot_inputs=out.projection.lot_inputs, daily=daily, window="cycle")
        c.update(ev=ev, violations=violations, candidate=candidate_from_evaluation(policy, ev, violations))
    ranking = rank_candidates(policy, [c["candidate"] for c in uniq])
    reports = {r["name"]: r for r in ranking["reports"]}
    rows = []
    for c in uniq:
        r = reports[c["name"]]; m = r.get("metrics") or {}; t = c["ev"].totals
        rows.append({
            "후보": c["name"], "별칭": " / ".join(c["aliases"]), "서비스 수준": c["service_level"], "여유율": c["buffer"],
            "변형": c["variant"], "실행 가능": r["feasible"], "불가 사유": "; ".join(r["violations"]),
            "발주 라인": t["n_order_lines"], "발주 금액": round(t["purchase_amount"]),
            "창 내 품절 확률": round(m.get("p_any_stockout", t["p_any_stockout"]), 3),
            "기대 품절 품목 수": round(t["mean_stockout_pairs"], 2),
            "기대 품절 손실매출": round(t["mean_lost_revenue"]), "기대 폐기 비용": round(t["mean_waste_value"]),
            "가중 평균 손실": None if not m else round(m["weighted_mean_loss"]),
            "가중 CVaR": None if not m else round(m["weighted_cvar"]),
            "점수": None if r["score"] is None else round(r["score"]), "비고": c["note"],
        })
    table = pd.DataFrame(rows)
    if len(table):
        table = table.sort_values(["실행 가능", "점수", "발주 금액"], ascending=[False, True, True], na_position="last").reset_index(drop=True)
    best = ranking["best"]["name"] if ranking.get("best") else None
    return dict(candidates=uniq, ranking=ranking, table=table, best=best, scenarios=scenarios,
                note="후보는 서비스 수준·여유율 격자와 최소금액 변형 안에서 비교한 것이며 전역 최적해가 아님")
