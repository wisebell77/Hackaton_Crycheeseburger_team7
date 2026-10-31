"""Promotion input contract v1. No discount elasticity is invented or fitted."""
from __future__ import annotations

import math
import re
from dataclasses import replace

import numpy as np
import pandas as pd


def _fields(obj, allowed, required, label):
    if not isinstance(obj, dict):
        raise ValueError(f"{label}: JSON object 필요")
    if set(obj) - set(allowed) or set(required) - set(obj):
        raise ValueError(f"{label}: 필드 오류, 허용 {sorted(allowed)}, 필수 {sorted(required)}")


def _number(value, label, low, high):
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value):
        raise ValueError(f"{label}: 유한한 숫자 필요")
    if not low <= value <= high:
        raise ValueError(f"{label}: {low}~{high} 범위 필요")
    return float(value)


def _day(value, label):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError(f"{label}: YYYY-MM-DD 필요")
    try:
        d = pd.Timestamp(value)
        if pd.isna(d):
            raise ValueError()
        return d
    except (ValueError, TypeError) as exc:
        raise ValueError(f"{label}: 유효한 날짜 필요") from exc


def _ids(value, valid, label):
    if (not isinstance(value, list) or not value or any(not isinstance(x, str) for x in value)
            or len(set(value)) != len(value) or set(value) - set(valid)):
        raise ValueError(f"{label}: 중복 없는 등록 ID의 비어 있지 않은 배열 필요")
    return sorted(value)


def _table(ds, key, cols):
    frame = ds.get(key)
    if frame is None or not set(cols) <= set(frame.columns):
        raise ValueError(f"{key}: 필수 컬럼 {cols} 필요")
    return frame


def validate_plan(payload, ds, params):
    """Validate before preview/save. Never mutate CSV/Dataset or guess menu IDs.

    Future plans are separate from factual 08_promotions. The cutoff is the
    engine's asof (end of business), not the computer clock.
    """
    _fields(payload, {"schema_version", "promotions"}, {"schema_version", "promotions"}, "plan")
    if type(payload["schema_version"]) is not int or payload["schema_version"] != 1:
        raise ValueError("schema_version: 1 필요")
    if not isinstance(payload["promotions"], list):
        raise ValueError("promotions: 배열 필요")
    menus = _table(ds, "02_menus", ["menu_id", "virtual_price"])
    stores = _table(ds, "01_stores", ["store_id"])
    av = _table(ds, "15_menu_availability", ["store_id", "menu_id", "start_date", "end_date", "status"])
    ids, plans = set(), []
    for obj in payload["promotions"]:
        allowed = {"promotion_id", "name", "store_ids", "target_menu_ids", "start_date", "end_date", "offer", "response"}
        _fields(obj, allowed, allowed, "promotion")
        pid = obj["promotion_id"]
        if not isinstance(pid, str) or not pid.strip() or pid in ids:
            raise ValueError("promotion_id: 비어 있지 않은 고유 문자열 필요")
        ids.add(pid)
        if not isinstance(obj["name"], str) or not obj["name"].strip():
            raise ValueError(f"{pid}: 행사 이름 필요")
        ss = _ids(obj["store_ids"], stores.store_id, "store_ids")
        ms = _ids(obj["target_menu_ids"], menus.menu_id, "target_menu_ids")
        start, end = _day(obj["start_date"], "start_date"), _day(obj["end_date"], "end_date")
        if start > end or end <= params.asof or (end - start).days > 365:
            raise ValueError(f"{pid}: 기준일 이후 날짜를 포함하고 시작≤종료, 최대 366일 필요")
        offer = obj["offer"]
        _fields(offer, {"type", "value"}, {"type", "value"}, "offer")
        if offer["type"] == "percent_off":
            value = _number(offer["value"], "할인율(%)", 0.000001, 100)
        elif offer["type"] == "amount_off":
            value = _number(offer["value"], "할인액(원)", 1, 1e9)
            prices = pd.to_numeric(menus.set_index("menu_id").loc[ms, "virtual_price"], errors="raise")
            if not np.isfinite(prices).all() or (prices < value).any() or value != int(value):
                raise ValueError("정액 할인은 정수 원이며 각 대상 메뉴 기본 가격 이하여야 함")
        else:
            raise ValueError("offer.type: percent_off 또는 amount_off만 지원; 1+1/쿠폰/세트 구성 변경은 미지원")
        response = obj["response"]
        if not isinstance(response, dict):
            raise ValueError("response: 객체 필요")
        if response.get("mode") == "manual":
            _fields(response, {"mode", "uplift_pct", "reason"}, {"mode", "uplift_pct", "reason"}, "response")
            uplift = _number(response["uplift_pct"], "예상 수요 변화율(%)", -100, 200)
            if not isinstance(response["reason"], str) or not response["reason"].strip():
                raise ValueError("예상 수요 변화율의 가정/근거 문장 필요")
            response = {**response, "uplift_pct": uplift}
        elif response.get("mode") == "historical_reference":
            fields = {"mode", "reference_promo_id", "accept_transfer_assumption"}
            _fields(response, fields, fields, "response")
            if response["accept_transfer_assumption"] is not True:
                raise ValueError("과거 행사를 미래 가정으로 전이한다는 명시적 확인 필요")
            promos = _table(ds, "08_promotions", ["promo_id", "store_id", "target_menu", "start_date", "end_date"])
            ref = promos[promos.promo_id == response["reference_promo_id"]]
            if len(ref) != 1:
                raise ValueError("reference_promo_id: 과거 행사 한 건을 정확히 선택해야 함")
            r = ref.iloc[0]
            if pd.isna(r.start_date) or pd.isna(r.end_date) or r.end_date > params.asof:
                raise ValueError("기준일까지 종료된 과거 행사만 참조 가능")
            if ss != [r.store_id] or set(ms) != set(r.target_menu):
                raise ValueError("과거 참고 모드는 동일 매장·동일 대상 메뉴 묶음만 허용; 다른 경우 수동 가정 입력")
        else:
            raise ValueError("response.mode: manual 또는 historical_reference 필요")
        # Explicit future non-availability cannot be overridden by a promotion.
        # Uncovered dates follow the existing engine's asof-state assumption.
        from src.engine.forecast import future_availability
        future_days = pd.date_range(max(start, params.asof + pd.Timedelta(days=1)), end)
        future = future_availability(ds, future_days, params.asof, [])
        selected = future[future.store_id.isin(ss) & future.menu_id.isin(ms)]
        if len(selected) != len(ss) * len(ms) * len(future_days) or not selected.is_available.eq(1).all():
            raise ValueError(f"{pid}: 행사 기간의 대상 메뉴가 취급 불가/확인 불가; 취급 계획을 먼저 수정하세요")
        plans.append({**obj, "store_ids": ss, "target_menu_ids": ms, "offer": {**offer, "value": value},
                      "response": response})
    for i, a in enumerate(plans):
        for b in plans[i + 1:]:
            if (set(a["store_ids"]) & set(b["store_ids"]) and set(a["target_menu_ids"]) & set(b["target_menu_ids"])
                    and a["start_date"] <= b["end_date"] and b["start_date"] <= a["end_date"]):
                raise ValueError("동일 매장·메뉴·날짜에 여러 행사 중첩 금지; 하나의 통합 시나리오로 입력하세요")
    return {"schema_version": 1, "promotions": plans}


def reference_response(ds, daily, plan, params):
    """Per-target descriptive DEMAND ratio, fixed 28-day pre-event baseline.

    No post-event observations enter the baseline. Demand, not net sales,
    is used consistently with the team's forecast engine. One event is n=1.
    """
    ref_id = plan["response"]["reference_promo_id"]
    row = ds.get("08_promotions").set_index("promo_id").loc[ref_id]
    start, end = row.start_date, row.end_date
    before = pd.date_range(start - pd.Timedelta(days=28), start - pd.Timedelta(days=1))
    event_dates = pd.date_range(start, end)
    result = {}
    for mid in plan["target_menu_ids"]:
        group = daily[(daily.store_id == row.store_id) & (daily.menu_id == mid) & (daily.date <= params.asof)]
        pre = group[group.date.isin(before) & group.is_available.eq(1) & group.is_holiday.eq(0) & group.promo_period.eq(0)]
        during = group[group.date.isin(event_dates)]
        wd = pre.groupby("weekday_iso").demand_qty.agg(["mean", "count"])
        dows = event_dates.dayofweek + 1
        if (len(during) != len(event_dates) or not during.is_available.eq(1).all()
                or during.is_holiday.eq(1).any() or not set(dows) <= set(wd.index)
                or any(wd.loc[w, "count"] < 3 for w in set(dows))):
            raise ValueError(f"{ref_id}/{mid}: 28일 같은 요일 기준 표본 부족 또는 행사 공휴일/취급 변동; 수동 가정을 입력하세요")
        expected = sum(float(wd.loc[w, "mean"]) for w in dows)
        if expected <= 0:
            raise ValueError(f"{ref_id}/{mid}: 비교 기준 수요가 0이어서 배율 계산 불가")
        factor = float(during.demand_qty.sum() / expected)
        if not math.isfinite(factor) or not 0 <= factor <= 3:
            raise ValueError("참고 배율이 제품 입력 범위 0~3을 벗어남; 자동 클립하지 않음")
        result[mid] = (factor, f"{ref_id}: 행사 수요 {during.demand_qty.sum():g}/사전 28일 동일요일 기준 {expected:g}; 독립 행사 1건")
    return result


def make_forecaster(payload, allow_compound_scenarios=False):
    """Return the same callable interface as src.engine.forecast.forecast_menu_daily.

    Use with run_engine(..., forecaster=make_forecaster(plan)). The returned
    forecast keeps the original columns and adds auditable promo fields.
    No writes to the source CSVs and no mutation of the supplied daily frame.
    """
    def forecast(ds, params, daily=None, notes=None):
        from src import prep
        from src.engine.forecast import forecast_menu_daily
        if notes is None:
            notes = []
        normalized = validate_plan(payload, ds, params)
        plans = normalized["promotions"]
        if plans and not params.apply_promo:
            raise ValueError("입력한 행사와 apply_promo=False가 충돌합니다. 행사 배열을 비우거나 행사를 활성화하세요")
        if plans and not allow_compound_scenarios and (params.demand_multiplier != 1 or any(v != 1 for v in params.menu_multipliers.values())):
            raise ValueError("행사 반응과 별도 수요 배율의 이중 적용 가능성: 중복 제거 또는 allow_compound_scenarios=True를 명시하세요")
        original = prep.daily_sales(ds) if daily is None else daily.copy()
        required = {"demand_qty", "promo_period", "promo_target", "is_available", "is_holiday", "weekday_iso"}
        if not required <= set(original):
            raise ValueError("daily는 prep.daily_sales(ds)의 demand_qty 및 행사/취급/달력 정보를 포함해야 합니다")
        legacy = ds.get("08_promotions")
        if legacy is not None:
            future_legacy = legacy[(legacy.end_date >= params.horizon_dates.min()) & (legacy.start_date <= params.horizon_dates.max())]
            # Existing/ongoing schedules also need an explicit response. Keep
            # their historical rows intact; never erase their past exposure.
            for event in future_legacy.itertuples():
                for day in pd.date_range(max(event.start_date, params.horizon_dates.min()), min(event.end_date, params.horizon_dates.max())):
                    for menu in event.target_menu:
                        if not any(event.store_id in p["store_ids"] and menu in p["target_menu_ids"]
                                   and p["start_date"] <= str(day.date()) <= p["end_date"] for p in plans):
                            raise ValueError(f"08_promotions 일정 {event.promo_id}: 예측 기간의 모든 대상 메뉴에 명시적 반응 계획이 필요합니다")
        # Whole-store promo days are excluded: observed non-target spillovers
        # should not be silently baked into the no-promotion baseline either.
        clean = original[original.promo_period.eq(0)].copy()
        fc, basis = forecast_menu_daily(ds, replace(params, apply_promo=False), daily=clean, notes=notes)
        fc["baseline_forecast_qty"] = fc.forecast_qty
        fc["promo_id"] = ""
        fc["promo_factor"] = 1.0
        fc["promo_active"] = 0
        fc["promo_response_source"] = "no_promotion"
        fc["promo_response_note"] = ""
        fc["offer_type"] = ""
        fc["offer_value"] = np.nan
        for plan in plans:
            if plan["response"]["mode"] == "historical_reference":
                response = reference_response(ds, original, plan, params)
                notes.append(f"{plan['promotion_id']}: 과거 행사 1건의 수요 변화 전이 가정; 할인율 반응/인과효과/미래 성능은 검증되지 않음")
            else:
                r = plan["response"]
                response = {m: (1 + r["uplift_pct"] / 100, r["reason"]) for m in plan["target_menu_ids"]}
            for mid, (factor, reason) in response.items():
                mask = (fc.store_id.isin(plan["store_ids"]) & fc.menu_id.eq(mid)
                        & fc.date.between(pd.Timestamp(plan["start_date"]), pd.Timestamp(plan["end_date"])))
                fc.loc[mask, "promo_id"] = plan["promotion_id"]
                fc.loc[mask, "promo_active"] = 1
                fc.loc[mask, "promo_factor"] = factor
                fc.loc[mask, "promo_response_source"] = plan["response"]["mode"]
                fc.loc[mask, "promo_response_note"] = reason
                fc.loc[mask, "offer_type"] = plan["offer"]["type"]
                fc.loc[mask, "offer_value"] = plan["offer"]["value"]
        fc["forecast_qty"] = fc.baseline_forecast_qty * fc.promo_factor
        fc["promo_increment_qty"] = fc.forecast_qty - fc.baseline_forecast_qty
        basis = basis.drop(columns=["forecast_total", "promo_factor", "promo_note"], errors="ignore")
        totals = fc.groupby(["store_id", "menu_id"]).agg(forecast_total=("forecast_qty", "sum"),
                                                           baseline_total=("baseline_forecast_qty", "sum"),
                                                           promo_increment_total=("promo_increment_qty", "sum")).reset_index()
        basis = basis.merge(totals, on=["store_id", "menu_id"], validate="one_to_one")
        basis["promo_note"] = "프로모션 배율은 날짜별 forecast 결과의 promo_factor/promo_response_source 참조"
        notes.append("기본 수요·공휴일 계산에서 과거 매장 행사일 전체를 제외; 과거 판매 이력은 기준일까지로 제한")
        notes.append("할인 조건은 행사 설명이며 수요 배율로 자동 변환하지 않음; 비대상 파급·종료 후 잔효과는 0 가정")
        notes.append("sigma는 기존 엔진의 비행사 이력 추정값; 행사 반응 자체의 불확실성은 미보정이며 서비스수준/품절확률 보장 아님")
        if allow_compound_scenarios:
            notes.append("행사 배율과 별도 수요 시나리오 배율의 복합 적용을 호출자가 명시적으로 허용함")
        return fc, basis
    return forecast
