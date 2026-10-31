"""
품절위험 라인의 대안 비교 (design_notes 2.7): 정기 발주 대기 / 긴급발주 / 매장 간 이동.

모두 대략적인 비교용이다 (가정 표시 필수).
- 품절 손실 ≈ 도착 전 부족량 ÷ (가장 많이 쓰는 메뉴의 1개당 사용량) × 그 메뉴 virtual_price  (가정: 부족분이 전부 그 메뉴 손실)
- 긴급발주: D+1 도착, 단가 × (1 + emergency_markup) (가정. 공급사 note 에 조건이 있으면 그 값으로 바꿀 것)
- 매장 간 이동: 같은 식자재 커버일수가 가장 큰 매장에서, 보낸 뒤에도 그 매장 커버일수가 도착까지 일수보다 크면 가능.
  비용 = transfer_cost (가정)
"""
from __future__ import annotations

import math
from typing import Any, Optional

import numpy as np
import pandas as pd

from .. import prep
from ..loader import Dataset
from .order import OrderResult
from .params import EngineParams
from .projection import ProjectionResult
from .requirement import menu_contributions

ALT_COLS = ["대안", "도착·가능 시점", "수량(사용단위)", "수량(발주단위)", "추가 비용(원)", "예상 품절 손실(원)", "합계(원)",
            "폐기 위험", "비고", "가정"]


def lost_sales_estimate(ds: Dataset, forecast_df: pd.DataFrame, store_id: str, ingredient_id: str,
                        shortage_qty: float) -> dict[str, Any]:
    """부족량(사용단위) → 못 파는 메뉴 수·금액 (가장 많이 쓰는 메뉴 기준, 대략)."""
    contrib = menu_contributions(forecast_df, ds, store_id, ingredient_id)
    if contrib.empty or shortage_qty <= 0:
        return {"menu_id": None, "menu_name": None, "lost_menus": 0.0, "lost_value": 0.0, "price": np.nan}
    top = contrib.iloc[0]
    menus = ds.get("02_menus")
    price = np.nan
    if menus is not None and "virtual_price" in menus.columns:
        p = menus.loc[menus["menu_id"] == top["menu_id"], "virtual_price"]
        price = float(p.iloc[0]) if len(p) else np.nan
    per = float(top["qty_per_menu"]) or 1.0
    lost = shortage_qty / per
    return {"menu_id": top["menu_id"], "menu_name": top["menu_name"], "lost_menus": lost,
            "lost_value": lost * price if pd.notna(price) else np.nan, "price": price}


def _units(ds: Dataset, iid: str, use_qty: float) -> tuple[int, float]:
    try:
        return prep.to_order_units(ds, iid, use_qty)
    except Exception:
        return 0, 0.0


def compare_alternatives(ds: Dataset, forecast_df: pd.DataFrame, projection: ProjectionResult, orders: OrderResult,
                         params: EngineParams, store_id: str, ingredient_id: str) -> pd.DataFrame:
    """한 매장×식자재 품절위험 라인의 대안 3개 표 (ALT_COLS)."""
    lines = orders.lines
    sel = lines[(lines["store_id"] == store_id) & (lines["ingredient_id"] == ingredient_id)]
    if sel.empty:
        return pd.DataFrame(columns=ALT_COLS)
    e = sel.iloc[0].to_dict()
    asof = params.asof
    u, ou = e.get("use_unit") or "", e.get("order_unit") or ""
    short = float(e.get("unmet_before_arrival1") or 0.0)
    price = float(e.get("unit_price")) if pd.notna(e.get("unit_price")) else np.nan
    lost = lost_sales_estimate(ds, forecast_df, store_id, ingredient_id, short)
    rows = []
    # 1) 정기 발주 대기
    lost_v = lost["lost_value"] if pd.notna(lost["lost_value"]) else 0.0
    rows.append({"대안": "정기 발주 대기", "도착·가능 시점": f"{pd.Timestamp(e['arrival1']):%m/%d} 도착",
                 "수량(사용단위)": f"{e['order_use_qty']:,.0f} {u}", "수량(발주단위)": f"{int(e['order_qty'])} {ou}",
                 "추가 비용(원)": 0.0, "예상 품절 손실(원)": lost_v, "합계(원)": lost_v,
                 "폐기 위험": "있음" if e.get("waste_risk") else "낮음",
                 "비고": (f"정기 발주 {float(e.get('amount') or 0):,.0f}원은 어느 대안이든 그대로 진행. 도착 전 부족 {short:,.0f}{u} ≈ "
                        f"{lost['menu_name']} {lost['lost_menus']:,.0f}개 × {lost['price']:,.0f}원 손실"
                        if short > 0 and lost["menu_name"] else "도착 전 부족 없음"),
                 "가정": "품절 손실 = 부족분 전부 최다 사용 메뉴 손실 (대략)"})
    # 2) 긴급발주 (D+1 도착 가정)
    eq, euse = _units(ds, ingredient_id, short)
    e_cost = eq * price * (1 + params.emergency_markup) if pd.notna(price) else np.nan
    rows.append({"대안": "긴급발주", "도착·가능 시점": f"{asof + pd.Timedelta(days=1):%m/%d} 도착(가정)",
                 "수량(사용단위)": f"{euse:,.0f} {u}", "수량(발주단위)": f"{eq} {ou}",
                 "추가 비용(원)": e_cost, "예상 품절 손실(원)": 0.0, "합계(원)": e_cost,
                 "폐기 위험": "중간" if euse > short * 1.5 and short > 0 else "낮음",
                 "비고": f"단가 {price:,.0f}원 × (1 + 할증 {params.emergency_markup:.0%}), 정기 발주와 별도",
                 "가정": f"익일 도착·할증 {params.emergency_markup:.0%} (데이터에 없음)"})
    # 3) 매장 간 이동
    rows.append(_transfer_row(projection, params, store_id, ingredient_id, short, e))
    return pd.DataFrame(rows, columns=ALT_COLS)


def _transfer_row(projection: ProjectionResult, params: EngineParams, store_id: str, ingredient_id: str,
                  short: float, e: dict[str, Any]) -> dict[str, Any]:
    """커버일수가 가장 큰 다른 매장에서 부족분을 옮기는 안."""
    u = e.get("use_unit") or ""
    s = projection.summary
    cand = s[(s["ingredient_id"] == ingredient_id) & (s["store_id"] != store_id)].copy()
    days_to_a1 = max(int((pd.Timestamp(e["arrival1"]) - params.asof).days), 1)
    base = {"대안": "매장 간 이동", "수량(발주단위)": "-", "가정": f"이동 비용 {params.transfer_cost:,.0f}원/건, 당일 이동 가능 (가정)"}
    if cand.empty or short <= 0:
        return {**base, "도착·가능 시점": "-", "수량(사용단위)": "-", "추가 비용(원)": np.nan, "예상 품절 손실(원)": np.nan,
                "합계(원)": np.nan, "폐기 위험": "-", "비고": "다른 매장 재고 없음" if short > 0 else "도착 전 부족 없음"}
    cand["cover_sort"] = cand["days_of_cover"].replace(np.inf, 1e9)
    donor = cand.sort_values("cover_sort", ascending=False).iloc[0]
    after = donor["on_hand_asof"] - short
    cover_after = after / donor["avg_daily_req"] if donor["avg_daily_req"] > 0 else (np.inf if after >= 0 else -np.inf)
    ok = after >= 0 and cover_after > days_to_a1
    note = (f"{donor['store_id']} 현재고 {donor['on_hand_asof']:,.0f}{u} (커버 {donor['days_of_cover']:.1f}일) → "
            f"이동 후 커버 {cover_after:.1f}일 {'>' if ok else '≤'} 도착까지 {days_to_a1}일")
    return {**base, "도착·가능 시점": f"{params.asof + pd.Timedelta(days=1):%m/%d} (가정)",
            "수량(사용단위)": f"{short:,.0f} {u}", "추가 비용(원)": float(params.transfer_cost) if ok else np.nan,
            "예상 품절 손실(원)": 0.0 if ok else np.nan, "합계(원)": float(params.transfer_cost) if ok else np.nan,
            "폐기 위험": "낮음" if ok else "-", "비고": ("가능: " if ok else "불가: 보내는 매장도 위험 — ") + note}
