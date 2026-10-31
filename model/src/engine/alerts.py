"""
재고 알림 (design_notes 2.6): 품절위험 / 사용기한임박 / 과잉. 제공 데이터의 low_stock_flag·overstock_flag 를 옆에 같이 둔다.

- 품절위험: 이번 발주 없이 전개했을 때 품절 예상일이 이번 발주 도착일(arrival1) 이전 → 높음
            (전개상 품절은 없지만 현재고 커버일수 < 도착까지 일수 → 입고 예정 PO 에 의존, 중간)
- 사용기한임박: 마지막 사용일이 D + expiry_alert_days 이내인 로트 중 그때까지 못 쓰는 양 > 0
- 과잉: 현재고 커버일수(현재고 ÷ 일평균 예측 소요) > overstock_cover_days
"""
from __future__ import annotations

import math
from typing import Any, Optional

import numpy as np
import pandas as pd

from .order import OrderResult
from .params import EngineParams
from .projection import ProjectionResult

ALERT_TYPES = ("품절위험", "사용기한임박", "과잉")
SEVERITY_RANK = {"높음": 0, "중간": 1, "낮음": 2}
ALERT_COLS = ["store_id", "ingredient_id", "ingredient_name", "supplier_id", "type", "severity", "severity_rank",
              "value_qty", "use_unit", "value_won", "stockout_date", "arrival1", "days_of_cover", "on_hand",
              "reason", "action", "data_low_stock_flag", "data_overstock_flag"]


def _fmt(v: Any) -> str:
    if v is None or (isinstance(v, float) and (math.isnan(v) or math.isinf(v))):
        return "-"
    return f"{v:,.0f}" if abs(float(v) - round(float(v))) < 1e-9 else f"{float(v):,.1f}"


def _md(t: Any) -> str:
    return "-" if t is None or pd.isna(t) else f"{pd.Timestamp(t):%m/%d}"


def build_alerts(projection: ProjectionResult, orders: OrderResult, params: EngineParams) -> pd.DataFrame:
    """알림 표 (ALERT_COLS). 심각도 → 금액 순 정렬."""
    s = projection.summary
    if s is None or s.empty:
        return pd.DataFrame(columns=ALERT_COLS)
    asof = params.asof
    lines = orders.lines if orders is not None and len(orders.lines) else pd.DataFrame()
    a1_map = {}
    if len(lines):
        a1_map = {(r.store_id, r.ingredient_id): (r.arrival1, r.unmet_before_arrival1, r.need_before_arrival1,
                                                   r.usable_before_arrival1, r.inbound_lines)
                  for r in lines.itertuples(index=False)}
    rows = []
    for r in s.itertuples(index=False):
        key = (r.store_id, r.ingredient_id)
        a1, unmet, need_a1, usable_a1, inb = a1_map.get(key, (pd.NaT, 0.0, 0.0, 0.0, []))
        base = {"store_id": r.store_id, "ingredient_id": r.ingredient_id, "ingredient_name": r.ingredient_name,
                "supplier_id": r.supplier_id, "use_unit": r.use_unit, "stockout_date": r.stockout_date,
                "arrival1": a1, "days_of_cover": r.days_of_cover, "on_hand": r.on_hand_asof,
                "data_low_stock_flag": getattr(r, "data_low_stock_flag", np.nan),
                "data_overstock_flag": getattr(r, "data_overstock_flag", np.nan)}
        price = getattr(r, "unit_price_per_use_unit", np.nan)
        # 품절위험: 높음 = 이번 발주 도착 전 품절 (FEFO 전개), 중간 = 입고 예정 PO 가 안 오면 도착 전 부족
        if pd.notna(r.stockout_date) and pd.notna(a1) and r.stockout_date < a1:
            short = float(unmet or 0.0)
            rows.append({**base, "type": "품절위험", "severity": "높음", "value_qty": short,
                         "value_won": short * price if pd.notna(price) else np.nan,
                         "reason": f"{_md(r.stockout_date)} 품절 예상, 이번 발주 도착 {_md(a1)} 전 부족 {_fmt(short)}{r.use_unit}",
                         "action": "긴급발주 또는 매장 간 이동 검토 (대안 비교 탭)"})
        elif pd.notna(a1) and need_a1 > usable_a1 + 1e-9:
            gap = need_a1 - usable_a1
            pos = ", ".join(f"{x['po_line_id']} {_md(x['arrival_date'])}" for x in (inb or [])[:2])
            rows.append({**base, "type": "품절위험", "severity": "중간", "value_qty": gap,
                         "value_won": gap * price if pd.notna(price) else np.nan,
                         "reason": f"입고 예정 PO({pos or '-'})가 안 오면 {_md(a1)} 도착 전 {_fmt(gap)}{r.use_unit} 부족",
                         "action": "입고 예정 PO 도착 확인 (지연 보정 슬라이더로 영향 확인)"})
        # 사용기한임박
        if r.expiring_qty > 1e-9:
            val = r.expiring_value
            rows.append({**base, "type": "사용기한임박", "severity": "높음" if (pd.notna(val) and val >= 50000) else "중간",
                         "value_qty": r.expiring_qty, "value_won": val,
                         "reason": f"{params.expiry_alert_days}일 안에 기한 끝나는 로트 중 {_fmt(r.expiring_qty)}{r.use_unit} 소진 불가"
                                   f" (≈{_fmt(val)}원)",
                         "action": "선사용·프로모션 또는 다른 매장으로 이동, 이번 발주량 축소"})
        # 과잉
        over = float(params.overstock_cover_days or 7.0)
        if np.isfinite(r.days_of_cover) and r.days_of_cover > over and r.on_hand_asof > 0:
            excess = r.on_hand_asof - over * r.avg_daily_req
            rows.append({**base, "type": "과잉", "severity": "중간" if r.days_of_cover > 2 * over else "낮음",
                         "value_qty": excess, "value_won": excess * price if pd.notna(price) else np.nan,
                         "reason": f"현재고 커버 {_fmt(r.days_of_cover)}일 > 기준 {_fmt(over)}일 (초과 {_fmt(excess)}{r.use_unit})",
                         "action": "다음 발주 축소/보류, 부족 매장으로 이동 후보"})
        elif not np.isfinite(r.days_of_cover) and r.on_hand_asof > 0:
            rows.append({**base, "type": "과잉", "severity": "낮음", "value_qty": r.on_hand_asof,
                         "value_won": r.on_hand_asof * price if pd.notna(price) else np.nan,
                         "reason": "예측 소요 0 인데 재고 있음 (미취급 메뉴 전용 식자재 등)",
                         "action": "다른 매장 이동 또는 발주 중단"})
    out = pd.DataFrame(rows, columns=[c for c in ALERT_COLS if c != "severity_rank"])
    if out.empty:
        return pd.DataFrame(columns=ALERT_COLS)
    out["severity_rank"] = out["severity"].map(SEVERITY_RANK)
    out = out.sort_values(["severity_rank", "value_won"], ascending=[True, False], na_position="last")
    return out[ALERT_COLS].reset_index(drop=True)


def flag_comparison(projection: ProjectionResult, params: EngineParams, low_cover_days: float) -> pd.DataFrame:
    """우리 기준 vs 제공 플래그 (기준일) 비교 표: 매장×식자재 1행."""
    s = projection.summary
    if s is None or s.empty:
        return pd.DataFrame()
    over = float(params.overstock_cover_days or 7.0)
    t = s[["store_id", "ingredient_id", "ingredient_name", "use_unit", "on_hand_asof", "days_of_cover",
           "data_closing_qty", "data_avg_consumption_7d", "data_low_stock_flag", "data_overstock_flag"]].copy()
    t["data_cover_days"] = t["data_closing_qty"] / t["data_avg_consumption_7d"].replace(0, np.nan)
    t["our_low"] = (t["days_of_cover"] < low_cover_days).astype(int)
    t["our_over"] = (t["days_of_cover"] > over).astype(int)
    return t
