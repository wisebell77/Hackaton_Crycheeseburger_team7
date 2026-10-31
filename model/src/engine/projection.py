"""
로트 단위 FEFO 재고 전개 (design_notes 2.4).

가정 (가정 목록에도 남김)
- 출발점: 기준일 D 영업 종료 후 로트 잔량 (prep.lots_asof). Σ 로트 잔량 = D 의 closing_qty.
- 입고 예정: D 까지 미입고 PO 라인 (prep.open_po_lines) 이 max(expected_date, D+1) + delay_buffer_days 에 도착.
  새 로트의 마지막 사용일 = 도착일 + shelf_life_days − 1 (입고일 포함).
- 하루 순서: 아침 입고 → 마지막 사용일이 지난 로트(last_use_date < 당일) 폐기 → 소요량을 마지막 사용일 빠른 로트부터 차감(FEFO)
  → 모자라면 부족(품절). 부족분은 이월하지 않는다(못 판 수요는 사라짐).
- 실사 조정·이동·기타 손실은 0 으로 본다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

import numpy as np
import pandas as pd

from .. import prep
from ..loader import Dataset
from .params import EngineParams

FAR_FUTURE = pd.Timestamp("2099-12-31")
LOT_COLS = ["store_id", "ingredient_id", "lot_id", "source", "arrival_date", "last_use_date", "qty"]


@dataclass
class ProjectionResult:
    """재고 전개 결과.

    daily    : store_id, ingredient_id, date, requirement, on_hand_start, received, expired_waste, consumed, shortage, on_hand_end
    summary  : 매장×식자재 1행 (현재고, 입고 예정, 소요 합계, 커버일수, 품절 예상일, 예상 폐기량·금액, 임박 미소진량, 제공 플래그)
    lots     : 로트별 시작 수량, 도착일, 마지막 사용일, 남은 일수, 기간 내 소진량·폐기량, 기간 말 잔량
    expiring : 마지막 사용일이 D + expiry_alert_days 이내인 로트와 그때까지 못 쓰는 양
    lot_inputs, requirement : 다시 전개할 때 쓰는 입력 (UI 에서 발주 수량을 바꿀 때)
    """

    daily: pd.DataFrame
    summary: pd.DataFrame
    lots: pd.DataFrame
    expiring: pd.DataFrame
    lot_inputs: pd.DataFrame
    requirement: pd.DataFrame
    dates: pd.DatetimeIndex
    asof: pd.Timestamp
    prices: pd.DataFrame = field(default_factory=pd.DataFrame)


def ingredient_master(ds: Dataset) -> pd.DataFrame:
    """식자재 마스터 (필요한 컬럼만, 없으면 빈 값)."""
    ing = ds.get("03_ingredients")
    cols = ["ingredient_id", "ingredient_name", "supplier_id", "use_unit", "order_unit", "use_qty_per_order_unit",
            "storage", "shelf_life_days", "min_order_qty"]
    if ing is None:
        return pd.DataFrame(columns=cols)
    out = ing.drop_duplicates("ingredient_id").copy()
    for c in cols:
        if c not in out.columns:
            out[c] = np.nan
    if out["ingredient_name"].isna().all():
        out["ingredient_name"] = out["ingredient_id"]
    return out[cols].reset_index(drop=True)


def _lots_from_closing(ds: Dataset, asof: pd.Timestamp, notes: Optional[list[str]]) -> pd.DataFrame:
    """16 로트 표가 없을 때: 10 의 기준일 closing_qty 를 로트 하나로 (마지막 사용일 = 기준일 + 사용기한 − 1, 가정)."""
    inv = ds.get("10_inventory_daily")
    if inv is None or not {"date", "store_id", "ingredient_id", "closing_qty"} <= set(inv.columns):
        return pd.DataFrame(columns=LOT_COLS)
    day = inv[(inv["date"] == asof) & (inv["closing_qty"] > 0)]
    if day.empty:
        return pd.DataFrame(columns=LOT_COLS)
    shelf = ingredient_master(ds).set_index("ingredient_id")["shelf_life_days"].astype(float)
    sl = day["ingredient_id"].map(shelf)
    last = (asof + pd.to_timedelta(sl.fillna(0) - 1, unit="D")).where(sl.notna(), FAR_FUTURE)
    if notes is not None:
        notes.append("기준일 로트(16) 기록이 없어 10_inventory_daily closing_qty 를 로트 하나로 보고, "
                     "마지막 사용일 = 기준일 + 사용기한 − 1 로 가정 (임박·폐기 과소 가능)")
    return pd.DataFrame({"store_id": day["store_id"].values, "ingredient_id": day["ingredient_id"].values,
                         "lot_id": ("CLOSING-" + day["store_id"] + "-" + day["ingredient_id"]).values, "source": "재고",
                         "arrival_date": asof, "last_use_date": last.values,
                         "qty": day["closing_qty"].astype(float).values})


def initial_lots(ds: Dataset, asof: pd.Timestamp, notes: Optional[list[str]] = None) -> pd.DataFrame:
    """기준일 영업 종료 후 로트 → 전개용 로트 표 (source='재고'). 로트 기록이 없으면 closing_qty 로 대체."""
    try:
        lots = prep.lots_asof(ds, asof)
    except Exception:
        lots = pd.DataFrame()
    if lots.empty:
        return _lots_from_closing(ds, asof, notes)
    out = pd.DataFrame({
        "store_id": lots["store_id"], "ingredient_id": lots["ingredient_id"], "lot_id": lots["lot_id"].astype(str),
        "source": "재고", "arrival_date": lots["receipt_date"] if "receipt_date" in lots.columns else asof,
        "last_use_date": lots["last_use_date"] if "last_use_date" in lots.columns else pd.NaT,
        "qty": lots["remaining_qty"].astype(float)})
    return out[out["qty"] > 0].reset_index(drop=True)


def inbound_lots(ds: Dataset, params: EngineParams, notes: Optional[list[str]] = None) -> pd.DataFrame:
    """미입고 PO 라인 → 도착 예정 로트 (source='입고예정')."""
    asof = params.asof
    try:
        po = prep.open_po_lines(ds, asof)
    except Exception:
        return pd.DataFrame(columns=LOT_COLS + ["po_id", "expected_date", "is_overdue"])
    if po.empty or "open_use_qty" not in po.columns:
        return pd.DataFrame(columns=LOT_COLS + ["po_id", "expected_date", "is_overdue"])
    shelf = ingredient_master(ds).set_index("ingredient_id")["shelf_life_days"]
    exp = po["expected_date"] if "expected_date" in po.columns else pd.Series(pd.NaT, index=po.index)
    next_day = asof + pd.Timedelta(days=1)
    arrival = exp.fillna(next_day).where(exp.fillna(next_day) >= next_day, next_day)
    arrival = arrival + pd.Timedelta(days=int(params.delay_buffer_days))
    sl = po["ingredient_id"].map(shelf).astype(float)
    last_use = arrival + pd.to_timedelta(sl.fillna(0) - 1, unit="D")
    last_use = last_use.where(sl.notna(), FAR_FUTURE)
    out = pd.DataFrame({
        "store_id": po["store_id"], "ingredient_id": po["ingredient_id"],
        "lot_id": po["po_line_id"].astype(str), "source": "입고예정", "arrival_date": arrival,
        "last_use_date": last_use, "qty": po["open_use_qty"].astype(float),
        "po_id": po.get("po_id"), "expected_date": exp, "is_overdue": po.get("is_overdue", False)})
    if bool(out["is_overdue"].fillna(False).any()):
        if notes is not None:
            notes.append("예정일이 기준일 이전인데 미입고인 PO 는 D+1 에 도착한다고 가정")
    return out[out["qty"] > 0].reset_index(drop=True)


def simulate_pair(lots: pd.DataFrame, req: pd.Series, dates: pd.DatetimeIndex) -> tuple[pd.DataFrame, pd.DataFrame]:
    """한 매장×식자재의 FEFO 일별 전개.

    lots : LOT_COLS 표 (이 매장×식자재 것만). arrival_date ≤ D 인 로트는 처음부터 보유.
    req  : date → 소요량 (use_unit)
    반환 : (일별 표, 로트 결과 표 [lot_id, consumed, wasted, waste_date, end_qty, arrived])
    """
    first = dates[0]
    state: list[dict[str, Any]] = []
    for r in lots.itertuples(index=False):
        lu = r.last_use_date if pd.notna(r.last_use_date) else FAR_FUTURE
        arr = r.arrival_date if pd.notna(r.arrival_date) else first
        state.append({"lot_id": r.lot_id, "arrival": arr, "last_use": lu, "qty": float(r.qty),
                      "on_hand": r.source == "재고" or arr < first, "consumed": 0.0, "wasted": 0.0,
                      "waste_date": pd.NaT})
    rows = []
    for d in dates:
        start = sum(s["qty"] for s in state if s["on_hand"])
        received = 0.0
        for s in state:
            if not s["on_hand"] and s["arrival"] <= d:
                s["on_hand"] = True
                received += s["qty"]
        waste = 0.0
        for s in state:
            if s["on_hand"] and s["qty"] > 0 and s["last_use"] < d:
                waste += s["qty"]
                s["wasted"] += s["qty"]
                s["waste_date"] = d
                s["qty"] = 0.0
        need = float(req.get(d, 0.0))
        left = need
        for s in sorted((s for s in state if s["on_hand"] and s["qty"] > 0), key=lambda x: (x["last_use"], x["arrival"])):
            if left <= 1e-12:
                break
            take = min(s["qty"], left)
            s["qty"] -= take
            s["consumed"] += take
            left -= take
        consumed = need - left
        end = sum(s["qty"] for s in state if s["on_hand"])
        rows.append((d, need, start, received, waste, consumed, max(left, 0.0), end))
    daily = pd.DataFrame(rows, columns=["date", "requirement", "on_hand_start", "received", "expired_waste",
                                        "consumed", "shortage", "on_hand_end"])
    lot_out = pd.DataFrame([{"lot_id": s["lot_id"], "consumed": s["consumed"], "wasted": s["wasted"],
                             "waste_date": s["waste_date"], "end_qty": s["qty"] if s["on_hand"] else 0.0,
                             "arrived": s["on_hand"]} for s in state],
                           columns=["lot_id", "consumed", "wasted", "waste_date", "end_qty", "arrived"])
    return daily, lot_out


def _req_lookup(requirement_df: pd.DataFrame) -> dict[tuple[str, str], pd.Series]:
    return {k: g.set_index("date")["requirement_qty"] for k, g in requirement_df.groupby(["store_id", "ingredient_id"])}


def pair_summary(daily: pd.DataFrame, lots: pd.DataFrame, lot_out: pd.DataFrame, asof: pd.Timestamp,
                 alert_days: int, price_use: float) -> dict[str, Any]:
    """한 매장×식자재 전개 결과 요약."""
    on_hand = float(lots.loc[lots["source"] == "재고", "qty"].sum()) if len(lots) else 0.0
    avg_req = float(daily["requirement"].mean()) if len(daily) else 0.0
    short = daily[daily["shortage"] > 1e-9]
    so_date = short["date"].iloc[0] if len(short) else pd.NaT
    m = lots.merge(lot_out, on="lot_id", how="left") if len(lots) else lots.assign(wasted=0.0)
    days_to = (m["last_use_date"] - asof).dt.days if len(m) else pd.Series(dtype=float)
    exp_mask = (days_to <= alert_days) & (m["source"] == "재고") if len(m) else pd.Series(dtype=bool)
    expiring_qty = float(m.loc[exp_mask, "wasted"].sum()) if len(m) else 0.0
    waste_qty = float(daily["expired_waste"].sum())
    inbound_in = float(daily["received"].sum())
    return {
        "on_hand_asof": on_hand, "inbound_in_horizon": inbound_in,
        "requirement_total": float(daily["requirement"].sum()), "avg_daily_req": avg_req,
        "days_of_cover": on_hand / avg_req if avg_req > 0 else np.inf,
        "stockout_date": so_date,
        "days_until_stockout": int((so_date - asof).days) if pd.notna(so_date) else None,
        "shortage_total": float(daily["shortage"].sum()),
        "waste_qty": waste_qty, "waste_value": waste_qty * price_use if pd.notna(price_use) else np.nan,
        "expiring_qty": expiring_qty,
        "expiring_value": expiring_qty * price_use if pd.notna(price_use) else np.nan,
        "on_hand_end": float(daily["on_hand_end"].iloc[-1]) if len(daily) else on_hand,
    }


def _dataset_flags(ds: Dataset, asof: pd.Timestamp) -> pd.DataFrame:
    """기준일의 제공 플래그·기말재고 (비교 표시용)."""
    inv = ds.get("10_inventory_daily")
    cols = ["store_id", "ingredient_id", "data_closing_qty", "data_avg_consumption_7d", "data_low_stock_flag",
            "data_overstock_flag"]
    if inv is None or "date" not in inv.columns:
        return pd.DataFrame(columns=cols)
    day = inv[inv["date"] == asof].drop_duplicates(["store_id", "ingredient_id"])
    out = pd.DataFrame({"store_id": day["store_id"], "ingredient_id": day["ingredient_id"]})
    for src, dst in (("closing_qty", "data_closing_qty"), ("avg_consumption_7d", "data_avg_consumption_7d"),
                     ("low_stock_flag", "data_low_stock_flag"), ("overstock_flag", "data_overstock_flag")):
        out[dst] = day[src].values if src in day.columns else np.nan
    return out.reset_index(drop=True)


def project_inventory(ds: Dataset, requirement_df: pd.DataFrame, params: EngineParams,
                      notes: Optional[list[str]] = None, extra_lots: Optional[pd.DataFrame] = None) -> ProjectionResult:
    """전 매장×식자재 FEFO 전개. extra_lots 를 주면 (추천 발주 등) 입고 로트로 더해서 전개한다."""
    asof = params.asof
    dates = params.horizon_dates
    init = initial_lots(ds, asof, notes)
    inb = inbound_lots(ds, params, notes)
    parts = [init, inb[LOT_COLS] if len(inb) else None, extra_lots[LOT_COLS] if extra_lots is not None and len(extra_lots) else None]
    lot_inputs = pd.concat([p for p in parts if p is not None and len(p)], ignore_index=True) \
        if any(p is not None and len(p) for p in parts) else pd.DataFrame(columns=LOT_COLS)
    prices = prep.price_table_asof(ds, asof) if ds.get("06_ingredient_price_history") is not None else pd.DataFrame()
    price_use = prices.set_index("ingredient_id")["unit_price_per_use_unit"].to_dict() if len(prices) else {}
    reqs = _req_lookup(requirement_df)
    pairs = set(reqs) | set(zip(lot_inputs["store_id"], lot_inputs["ingredient_id"]))
    lots_by_pair = {k: g for k, g in lot_inputs.groupby(["store_id", "ingredient_id"])}
    empty_lots = pd.DataFrame(columns=LOT_COLS)
    daily_parts, sums, lot_parts = [], [], []
    for (sid, iid) in sorted(pairs):
        lp = lots_by_pair.get((sid, iid), empty_lots)
        d, lo = simulate_pair(lp, reqs.get((sid, iid), pd.Series(dtype=float)), dates)
        daily_parts.append(d.assign(store_id=sid, ingredient_id=iid))
        s = pair_summary(d, lp, lo, asof, params.expiry_alert_days, price_use.get(iid, np.nan))
        sums.append({"store_id": sid, "ingredient_id": iid, **s})
        if len(lp):
            lot_parts.append(lp.merge(lo, on="lot_id", how="left", validate="one_to_one"))
    daily = pd.concat(daily_parts, ignore_index=True) if daily_parts else pd.DataFrame()
    daily = daily[["store_id", "ingredient_id", "date", "requirement", "on_hand_start", "received", "expired_waste",
                   "consumed", "shortage", "on_hand_end"]] if len(daily) else daily
    summary = pd.DataFrame(sums)
    master = ingredient_master(ds)
    if len(summary):
        summary = summary.merge(master, on="ingredient_id", how="left", validate="many_to_one")
        summary = summary.merge(_dataset_flags(ds, asof), on=["store_id", "ingredient_id"], how="left",
                                validate="one_to_one")
        summary["unit_price_per_use_unit"] = summary["ingredient_id"].map(price_use)
    lots = pd.concat(lot_parts, ignore_index=True) if lot_parts else pd.DataFrame(columns=LOT_COLS)
    if len(lots):
        lots["days_to_expiry"] = (lots["last_use_date"] - asof).dt.days
        lots["expires_in_horizon"] = lots["last_use_date"] < dates[-1]
    expiring = lots[(lots["source"] == "재고") & (lots["days_to_expiry"] <= params.expiry_alert_days)].copy() \
        if len(lots) else lots
    if notes is not None:
        for t in ("재고 전개: 기준일 로트 잔량에서 출발, 아침 입고 → 기한 지난 로트 폐기 → FEFO 소진, 부족분은 이월하지 않음",
                  f"입고 예정 PO 는 예정일 + 지연 보정 {params.delay_buffer_days}일에 도착, 새 로트 마지막 사용일 = 도착일 + 사용기한 − 1",
                  "재고 전개에서 실사 조정·매장 간 이동·기타 손실은 0 으로 가정"):
            if t not in notes:
                notes.append(t)
    return ProjectionResult(daily, summary, lots, expiring, lot_inputs, requirement_df, dates, asof, prices)


def reproject_pair(proj: ProjectionResult, store_id: str, ingredient_id: str, extra_lots: Iterable[dict[str, Any]] = (),
                   alert_days: int = 3) -> tuple[pd.DataFrame, dict[str, Any]]:
    """저장된 입력으로 한 매장×식자재만 다시 전개 (추가 입고 로트 포함). UI 의 발주 수량 수정용.

    extra_lots: [{'lot_id', 'arrival_date', 'last_use_date', 'qty', 'source'}]
    반환: (일별 표, 요약 dict)
    """
    li = proj.lot_inputs
    lp = li[(li["store_id"] == store_id) & (li["ingredient_id"] == ingredient_id)]
    extra = pd.DataFrame([{"store_id": store_id, "ingredient_id": ingredient_id, "source": "추천발주", **e}
                          for e in extra_lots if float(e.get("qty", 0) or 0) > 0], columns=LOT_COLS)
    lp = pd.concat([lp, extra], ignore_index=True) if len(extra) else lp
    req = proj.requirement
    r = req[(req["store_id"] == store_id) & (req["ingredient_id"] == ingredient_id)].set_index("date")["requirement_qty"]
    d, lo = simulate_pair(lp, r, proj.dates)
    price = np.nan
    if len(proj.prices):
        p = proj.prices.loc[proj.prices["ingredient_id"] == ingredient_id, "unit_price_per_use_unit"]
        price = float(p.iloc[0]) if len(p) else np.nan
    return d, pair_summary(d, lp, lo, proj.asof, alert_days, price)
