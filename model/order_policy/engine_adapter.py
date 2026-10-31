"""연결 대상: 저장소 두 ZIP의 src.engine.EngineParams / evaluate_plan.

기존 엔진 소스를 바꾸지 않는다. 엔진은 하나의 Python 프로세스에서 하나만 선택한다.
"""
from __future__ import annotations

import importlib
import math
import sys
from dataclasses import replace
from pathlib import Path


def load_engine(engine_dir):
    root = Path(engine_dir).resolve()
    if not (root / "src" / "engine" / "params.py").exists():
        raise ValueError("압축 해제한 엔진 폴더를 지정하세요")
    if "src" in sys.modules and not Path(sys.modules["src"].__file__).resolve().is_relative_to(root):
        raise ValueError("다른 src 패키지가 로드됨. 별도 프로세스에서 엔진을 선택하세요")
    sys.path.insert(0, str(root))
    return importlib.import_module("src")


def catalog_from_dataset(ds):
    return {kind: dict(zip(ds.get(table)[id_col], ds.get(table)[name_col]))
            for kind, table, id_col, name_col in (
                ("stores", "01_stores", "store_id", "store_name"),
                ("ingredients", "03_ingredients", "ingredient_id", "ingredient_name"))}


def scope_dataset(ds, policy):
    stores = policy["constraints"]["store_ids"]
    if stores is None:
        return ds
    # 현재 매장 범위 밖의 재고/수요가 평가 확률과 예산에 섞이지 않게 한다.
    tables = {k: (v[v["store_id"].isin(stores)].copy() if v is not None and "store_id" in v else v)
              for k, v in ds.tables.items()}
    return replace(ds, tables=tables)


def engine_params(policy, **base):
    from src.engine.params import EngineParams
    if policy["status"] != "ready":
        raise ValueError("미해결 자연어 조건이 있어 엔진 실행을 차단함")
    base.update(service_level=policy["preferences"]["service_level"], **policy["scenario"])
    return EngineParams(**base)


def buffered_plan(engine_output, policy):
    """여유율은 '커버 구간 총수요'에 적용하고 재고/입고를 빼고 포장 올림.

    net_need_use = max(0, gross_need - usable - inbound)라서 0에서 여유를
    더하면 과발주할 수 있다. 기존 설명값에서 음수까지 포함한 순필요량을 재구성한다.
    """
    import numpy as np
    from src.engine.evaluate import plan_from_orders
    lines = engine_output.orders.lines.copy()
    if lines.empty:
        return plan_from_orders(lines)
    b = policy["preferences"]["extra_demand_buffer_ratio"]
    net = ((lines["demand_window"] - lines["unmet_before_arrival1"] + lines["safety_stock"])
           + b * lines["demand_window"] - lines["available"] - lines["inbound"]).clip(lower=0)
    units = np.ceil(np.round(net / lines["use_qty_per_order_unit"], 9)).astype(int)
    moq = lines["min_order_qty"].fillna(1).clip(lower=1).astype(int)
    lines["order_qty"] = np.where(units > 0, np.maximum(units, moq), 0)
    exclude = policy["constraints"]["excluded_ingredient_ids"]
    lines.loc[lines.ingredient_id.isin(exclude), "order_qty"] = 0
    return plan_from_orders(lines)


def check_operational(ds, policy, raw_plan, params):
    """엔진의 normalize_plan은 소수를 int로 잘라내므로 먼저 원본 수량을 검사.

    자연어가 바꿀 수 없는 제약: ID, 단위, 정수 포장, MOQ, 공급사 달력,
    매장×공급사×발주일×정기/긴급별 최소금액, 현재 의사결정 이전 발주 금지.
    """
    import pandas as pd
    from src import prep
    from src.engine.evaluate import normalize_plan
    violations = []
    required = {"store_id", "ingredient_id", "order_qty"}
    if not required <= set(raw_plan.columns):
        return None, ["발주안 필수 열 누락"]
    p = raw_plan.copy()
    if p.empty:
        return normalize_plan(ds, p, params), []
    quantities = pd.to_numeric(p.order_qty, errors="coerce")
    if any(not math.isfinite(x) or x < 0 or x != int(x) for x in quantities):
        return None, ["발주 수량은 0 이상의 정수 포장 단위여야 함"]
    p = p[quantities > 0].copy()
    if p.empty:
        return normalize_plan(ds, p, params), []
    cat = catalog_from_dataset(ds)
    if not set(p.store_id) <= set(cat["stores"]) or not set(p.ingredient_id) <= set(cat["ingredients"]):
        return None, ["존재하지 않거나 선택 범위 밖의 매장/식자재 ID"]
    c = policy["constraints"]
    if c["store_ids"] is not None and not set(p.store_id) <= set(c["store_ids"]):
        violations.append("선택 매장 범위 밖의 발주")
    if set(p.ingredient_id) & set(c["excluded_ingredient_ids"]):
        violations.append("금지 식자재 포함")
    if "order_type" in p:
        kinds = p.order_type.fillna("정기")
        if not set(kinds) <= {"정기", "긴급"}:
            violations.append("알 수 없는 발주 유형")
        if not c["allow_emergency"] and (kinds == "긴급").any():
            violations.append("긴급발주 금지")
    try:
        cal = prep.SupplierCalendar.from_dataset(ds)
        normalized = normalize_plan(ds, p, params, cal=cal)
        if cal.warnings:
            violations.extend("공급사 정보 불완전: " + w for w in cal.warnings)
    except (ValueError, KeyError, TypeError) as exc:
        return None, violations + [f"발주안 정규화 실패: {exc}"]
    if normalized[["unit_price", "use_qty_per_order_unit", "amount"]].isna().any().any():
        violations.append("단가/단위 환산값 누락: 금액을 0으로 대신할 수 없음")
    if any(not math.isfinite(v) for v in normalized[["unit_price", "use_qty_per_order_unit", "amount"]].to_numpy().ravel()):
        violations.append("단가/단위/금액은 유한 숫자여야 함")
    if (normalized.use_qty_per_order_unit <= 0).any() or (normalized.unit_price < 0).any():
        violations.append("잘못된 단가/단위 환산값")
    moqs = ds.get("03_ingredients").set_index("ingredient_id")["min_order_qty"].to_dict()
    # 같은 품목을 여러 줄로 나눈 경우 MOQ는 합산 수량에 적용한다.
    for key, part in normalized.groupby(["store_id", "ingredient_id", "order_date", "order_type"]):
        if part.order_qty.sum() < moqs[key[1]]:
            violations.append(f"품목 최소발주량 미달: {key[1]}")
    for r in normalized.itertuples():
        if r.order_date < params.decision_datetime.normalize():
            violations.append("발주일이 현재 의사결정일 이전")
        if r.order_type == "정기":
            earliest = cal.next_order_datetime(r.supplier_id, params.decision_datetime).normalize()
            if r.order_date < earliest or not cal.is_order_day(r.supplier_id, r.order_date):
                violations.append(f"발주 요일/마감 위반: {r.supplier_id}")
            if r.arrival_date != cal.arrival_date(r.supplier_id, r.order_date):
                violations.append(f"공급사 입고 일정 위반: {r.supplier_id}")
        elif r.arrival_date != r.order_date + pd.Timedelta(days=1):
            violations.append("긴급 입고일은 기존 엔진의 익일 도착 가정과 일치해야 함")
    for (store, supplier, day, kind), part in normalized.groupby(["store_id", "supplier_id", "order_date", "order_type"]):
        minimum = cal.terms(supplier).min_order_amount
        if part.amount.isna().any():
            continue
        if 0 < part.amount.sum() < minimum:
            violations.append(f"공급사 최소금액 미달: {store}/{supplier}/{day.date()}/{kind}, 최소 {minimum:g}원")
    return normalized, list(dict.fromkeys(violations))


def candidate_from_evaluation(policy, ev, violations):
    return dict(name=ev.name, purchase_amount=ev.totals["purchase_amount"],
                scenarios=ev.scenarios[["lost_revenue", "waste_value", "any_stockout"]].to_dict("records"),
                operational_violations=violations)


def evaluate_with_policy(ds, out, plan, policy, *, scenarios, name="사용자 발주안", daily=None):
    """순서가 중요: 원본 수량/운영 제약 확인 → 같은 시나리오 평가 → 하드 제약/점수."""
    from src.engine.evaluate import evaluate_plan
    from .policy import assess_candidate
    _, violations = check_operational(ds, policy, plan, out.params)
    if violations:
        return dict(name=name, feasible=False, violations=violations, score=None), None
    ev = evaluate_plan(ds, out.params, out.forecast, out.requirement, plan,
                       scenarios=scenarios, name=name, lot_inputs=out.projection.lot_inputs,
                       daily=daily, window="cycle")
    return assess_candidate(policy, candidate_from_evaluation(policy, ev, [])), ev
