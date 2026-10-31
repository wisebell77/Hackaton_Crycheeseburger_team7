"""
공급사 조건을 반영한 발주 추천 (design_notes 2.5).

공급사 p, 매장 s, 식자재 i:
  plan          = SupplierCalendar.order_plan(p, D+1 decision_time) → 마감 t_order, 도착 arrival1, 다음 도착 arrival2
  커버 구간      = [D+1, arrival2)    (window_days 일)
  demand_window = Σ R[s,i,t]  (커버 구간)
  unmet_before  = FEFO 전개(이번 발주 없음)에서 arrival1 전에 생기는 부족분 — 이번 발주로 막을 수 없고 이월되지 않는 수요
  safety        = z(service_level) × σ_i(window_days)   σ = 식자재 단위 백테스트 잔차 RMSE(커버일수별), 급변 품목 × 1.6
                  (sigma_method="menu_sum" 이면 이전처럼 σ_daily × √window_days)
  available     = D 기말 로트 잔량 − 그중 arrival2 전에 기한이 지나 버려질 양 (FEFO 전개 결과로 계산, 명시적 계산)
  inbound       = arrival2 전에 도착하는 미입고 PO 사용단위 수량 (기한 경과분 제외)
  net_need_use  = max(0, demand_window − unmet_before + safety − available − inbound)
  → prep.add_order_units (발주단위 올림 + 최소발주수량) → 단가 prep.price_table_asof(발주일) → 금액
폐기 위험: 발주 사용단위 수량 > (arrival1 부터 사용기한 동안의 소요 − arrival1 시점 재고) 이면 '폐기 위험' (자르지 않고 둘 다 표시).
발주서(매장×공급사) 합계 vs min_order_amount → 충족 / 미달(부족 금액) / 제한 없음 / 발주 없음.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from statistics import NormalDist
from typing import Any, Optional

import numpy as np
import pandas as pd

from .. import prep
from ..loader import Dataset
from .params import EngineParams
from .projection import ProjectionResult, ingredient_master, reproject_pair

WEEKDAY_KO = "월화수목금토일"
EXPLAIN_KEYS = (
    "store_id", "ingredient_id", "ingredient_name", "supplier_id", "supplier_name", "use_unit", "order_unit",
    "decision_datetime", "order_deadline", "arrival1", "arrival2", "plan_text", "window_start", "window_end",
    "window_days", "daily_requirement", "demand_window", "unmet_before_arrival1", "service_level", "z",
    "sigma_daily", "sigma_window", "sigma_method", "sigma_note", "shift_flag", "shift_ratio", "safety_stock", "on_hand", "n_lots", "expiring_excluded", "expiring_lots", "available",
    "inbound", "inbound_lines", "net_need_use", "use_qty_per_order_unit", "raw_order_units", "ceil_order_units",
    "min_order_qty", "order_qty", "order_use_qty", "unit_price", "price_valid_from", "amount", "shelf_life_days",
    "shelf_requirement", "shelf_extrapolated_days", "stock_at_arrival1", "consumable_capacity", "waste_risk",
    "waste_risk_qty", "stockout_date_base", "stockout_date_after", "covered_until_arrival2", "waste_after_qty",
    "new_lot_waste_qty", "need_before_arrival1", "usable_before_arrival1",
)


@dataclass
class OrderResult:
    """발주 추천 결과.

    lines : 매장×식자재 1행 (공급사, 각 항 숫자, 발주수량, 금액, 폐기위험, 발주 반영 전개 결과, explain dict)
    pos   : 매장×공급사 발주서 1행 (plan 문장, 마감, 도착일, 총액, 최소발주금액, 상태, 부족 금액, 오늘 마감 여부)
    plans : {supplier_id: OrderPlan}
    """

    lines: pd.DataFrame
    pos: pd.DataFrame
    plans: dict[str, Any] = field(default_factory=dict)


def z_value(service_level: float) -> float:
    """서비스 수준 → 표준정규 z (0.95 → 1.645)."""
    sl = min(max(float(service_level), 0.5), 0.9999)
    return float(NormalDist().inv_cdf(sl))


def po_status(total_amount: float, min_order_amount: float) -> tuple[str, float]:
    """발주서 합계 vs 최소발주금액 → (상태, 부족 금액)."""
    total = float(total_amount or 0.0)
    moa = float(min_order_amount or 0.0)
    if total <= 0:
        return "발주 없음", 0.0
    if moa <= 0:
        return "제한 없음", 0.0
    if total >= moa:
        return "충족", 0.0
    return "미달", moa - total


def supplier_plans(ds: Dataset, params: EngineParams, notes: Optional[list[str]] = None) -> dict[str, Any]:
    """공급사별 OrderPlan (prep.SupplierCalendar 재사용). 계산 못 하는 공급사는 빠지고 notes 에 남긴다."""
    cal = prep.SupplierCalendar.from_dataset(ds)
    plans: dict[str, Any] = {}
    for sid in cal.supplier_ids:
        try:
            plans[sid] = cal.order_plan(sid, params.decision_datetime)
        except Exception as e:  # 요일 정보가 깨진 공급사 하나 때문에 전체가 멈추지 않게
            if notes is not None:
                notes.append(f"{sid}: 발주 일정 계산 실패 ({e}) → 발주 추천 제외")
    if notes is not None:
        for w in cal.warnings:
            if w not in notes:
                notes.append(w)
    plans["_calendar"] = cal
    return plans


def _window_sum(req: pd.Series, start: pd.Timestamp, end: pd.Timestamp) -> float:
    """start ≤ date < end 의 소요량 합."""
    if req.empty:
        return 0.0
    return float(req[(req.index >= start) & (req.index < end)].sum())


def _shelf_requirement(req: pd.Series, a1: pd.Timestamp, shelf: float, last_date: pd.Timestamp,
                       avg: float) -> tuple[float, int]:
    """arrival1 부터 사용기한(shelf) 일 동안의 소요. 예측 기간 밖은 일평균 소요로 외삽 (외삽 일수도 반환)."""
    if pd.isna(shelf) or shelf <= 0:
        return float("inf"), 0
    end = a1 + pd.Timedelta(days=int(shelf))  # [a1, a1+shelf) = 입고일 포함 shelf 일
    inside = _window_sum(req, a1, end)
    beyond = max(0, int((end - (last_date + pd.Timedelta(days=1))).days))
    return inside + avg * beyond, beyond


def _line_for(ds: Dataset, params: EngineParams, proj: ProjectionResult, plan: Any, sup_name: str,
              row: pd.Series, sig_fn: Any, z: float) -> dict[str, Any]:
    """한 매장×식자재의 발주 계산 항 (발주단위 환산 전)."""
    sid, iid = row["store_id"], row["ingredient_id"]
    asof = params.asof
    w_start = asof + pd.Timedelta(days=1)
    a1, a2 = plan.arrival1, plan.arrival2
    req_df = proj.requirement
    req = req_df[(req_df["store_id"] == sid) & (req_df["ingredient_id"] == iid)].set_index("date")["requirement_qty"]
    daily = proj.daily[(proj.daily["store_id"] == sid) & (proj.daily["ingredient_id"] == iid)].set_index("date")
    lots = proj.lots[(proj.lots["store_id"] == sid) & (proj.lots["ingredient_id"] == iid)] if len(proj.lots) else proj.lots
    window_days = max(int((a2 - w_start).days), 1)
    demand = _window_sum(req, w_start, a2)
    unmet = float(daily.loc[(daily.index >= w_start) & (daily.index < a1), "shortage"].sum()) if len(daily) else 0.0
    init = lots[lots["source"] == "재고"] if len(lots) else lots
    on_hand = float(init["qty"].sum()) if len(init) else 0.0
    exp_mask = init["waste_date"].notna() & (init["waste_date"] < a2) if len(init) else pd.Series(dtype=bool)
    expiring_excluded = float(init.loc[exp_mask, "wasted"].sum()) if len(init) else 0.0
    expiring_lots = [{"lot_id": r.lot_id, "last_use_date": r.last_use_date, "qty": float(r.qty), "wasted": float(r.wasted)}
                     for r in init[exp_mask].itertuples(index=False)] if len(init) else []
    exp_a1 = init["waste_date"].notna() & (init["waste_date"] < a1) if len(init) else pd.Series(dtype=bool)
    usable_before_a1 = on_hand - (float(init.loc[exp_a1, "wasted"].sum()) if len(init) else 0.0)
    inb = lots[(lots["source"] == "입고예정") & (lots["arrival_date"] < a2)] if len(lots) else lots
    inbound = float((inb["qty"] - inb["wasted"].where(inb["waste_date"] < a2, 0.0).fillna(0.0)).sum()) if len(inb) else 0.0
    inbound_lines = [{"po_line_id": r.lot_id, "arrival_date": r.arrival_date, "qty": float(r.qty)}
                     for r in inb.itertuples(index=False)] if len(inb) else []
    sigma_window, sinfo = sig_fn(sid, iid, window_days)
    safety = z * float(sigma_window)
    available = on_hand - expiring_excluded
    net = max(0.0, demand - unmet + safety - available - inbound)
    stock_a1 = float(daily.loc[a1, "on_hand_start"] + daily.loc[a1, "received"]) if a1 in daily.index else available
    shelf_req, extra_days = _shelf_requirement(req, a1, row.get("shelf_life_days"), proj.dates[-1],
                                               float(row.get("avg_daily_req", 0.0) or 0.0))
    return {
        "store_id": sid, "ingredient_id": iid, "ingredient_name": row.get("ingredient_name", iid),
        "supplier_id": plan.supplier_id, "supplier_name": sup_name, "storage": row.get("storage"),
        "use_unit": row.get("use_unit"), "order_unit": row.get("order_unit"),
        "decision_datetime": params.decision_datetime, "order_deadline": plan.order1_deadline,
        "arrival1": a1, "arrival2": a2, "plan_text": plan.explain(),
        "window_start": w_start, "window_end": a2 - pd.Timedelta(days=1), "window_days": window_days,
        "daily_requirement": [(d, float(v)) for d, v in req[(req.index >= w_start) & (req.index < a2)].items()],
        "demand_window": demand, "unmet_before_arrival1": unmet,
        "need_before_arrival1": _window_sum(req, w_start, a1), "usable_before_arrival1": usable_before_a1, "service_level": float(params.service_level), "z": z,
        "sigma_daily": float(sinfo.get("sigma_daily", 0.0)), "sigma_window": float(sigma_window),
        "sigma_method": sinfo.get("sigma_method"), "sigma_note": sinfo.get("sigma_note"),
        "shift_flag": bool(sinfo.get("shift_flag", False)), "shift_ratio": sinfo.get("shift_ratio"),
        "safety_stock": safety, "on_hand": on_hand, "n_lots": int(len(init)),
        "expiring_excluded": expiring_excluded, "expiring_lots": expiring_lots, "available": available,
        "inbound": inbound, "inbound_lines": inbound_lines, "net_need_use": net,
        "shelf_life_days": row.get("shelf_life_days"), "shelf_requirement": shelf_req,
        "shelf_extrapolated_days": extra_days, "stock_at_arrival1": stock_a1,
        "stockout_date_base": row.get("stockout_date"), "avg_daily_req": float(row.get("avg_daily_req", 0.0) or 0.0),
    }


def _new_lot(e: dict[str, Any], order_use_qty: float) -> dict[str, Any]:
    """추천 발주를 arrival1 에 도착하는 새 로트로."""
    shelf = e.get("shelf_life_days")
    a1 = e["arrival1"]
    lu = a1 + pd.Timedelta(days=int(shelf) - 1) if pd.notna(shelf) and shelf else pd.Timestamp("2099-12-31")
    return {"lot_id": "추천발주", "arrival_date": a1, "last_use_date": lu, "qty": float(order_use_qty)}


def evaluate_order_qty(proj: ProjectionResult, e: dict[str, Any], order_qty: int, alert_days: int = 3) -> dict[str, Any]:
    """발주 수량(발주단위)을 바꿨을 때 금액·폐기위험·품절 예상일·기간 내 폐기량을 다시 계산.

    e: 발주 라인의 explain dict (use_qty_per_order_unit, unit_price, arrival1, shelf_life_days 등 필요)
    """
    q = max(int(order_qty or 0), 0)
    conv = float(e.get("use_qty_per_order_unit") or 0.0)
    use_qty = q * conv
    price = e.get("unit_price")
    amount = q * float(price) if price is not None and pd.notna(price) else np.nan
    extra = [_new_lot(e, use_qty)] if use_qty > 0 else []
    daily, s = reproject_pair(proj, e["store_id"], e["ingredient_id"], extra, alert_days)
    capacity = max(0.0, float(e.get("shelf_requirement", np.inf)) - float(e.get("stock_at_arrival1", 0.0)))
    risk = bool(use_qty > capacity + 1e-9 and use_qty > 0)
    a1, a2 = pd.Timestamp(e["arrival1"]), pd.Timestamp(e["arrival2"])
    sh = daily[(daily["date"] >= a1) & (daily["shortage"] > 1e-9)]
    so_arr = sh["date"].iloc[0] if len(sh) else pd.NaT  # 이번 발주 도착 이후 첫 품절일
    covered = bool(pd.isna(so_arr) or so_arr >= a2)
    return {"order_qty": q, "order_use_qty": use_qty, "amount": amount, "consumable_capacity": capacity,
            "waste_risk": risk, "waste_risk_qty": max(0.0, use_qty - capacity) if risk else 0.0,
            "stockout_date_after": so_arr, "covered_until_arrival2": covered, "shortage_after": s["shortage_total"],
            "waste_after_qty": s["waste_qty"], "waste_after_value": s["waste_value"], "daily": daily}


def _sigma_lookup(sigma_df: Optional[pd.DataFrame], params: EngineParams) -> Any:
    """(매장, 식자재, 커버일수 L) → (σ_L, 설명 dict).

    sigma_df 에 sigma_by_L(백테스트 RMSE 목록)이 있으면 L 번째 값(목록보다 길면 마지막 값 × √(L/n) 외삽),
    shift_flag 면 × params.shift_sigma_mult. 없으면 sigma_daily × √L (메뉴 σ 합산 방식).
    """
    table: dict[tuple[str, str], dict[str, Any]] = {}
    if sigma_df is not None and len(sigma_df):
        table = {(r["store_id"], r["ingredient_id"]): r for r in sigma_df.to_dict("records")}
    mult_flag = float(params.shift_sigma_mult)

    def fn(sid: str, iid: str, L: int) -> tuple[float, dict[str, Any]]:
        r = table.get((sid, iid))
        if r is None:
            return 0.0, {"sigma_daily": 0.0, "sigma_method": "없음", "sigma_note": "σ 정보 없음 → 0",
                         "shift_flag": False, "shift_ratio": None}
        by_L = r.get("sigma_by_L")
        if isinstance(by_L, (list, tuple)) and len(by_L):
            n = len(by_L)
            s = float(by_L[L - 1]) if L <= n else float(by_L[-1]) * math.sqrt(L / n)
            flag = bool(r.get("shift_flag"))
            mult = mult_flag if flag else 1.0
            ratio = r.get("shift_ratio")
            note = f"식자재 단위 백테스트 잔차 RMSE({L}일 합계" + (f", {n}일 값에서 √ 외삽" if L > n else "") + ")"
            if flag:
                note += f" × {mult:.1f} 급변 중" + (f"(최근 2주/8주 소요 비율 {ratio:.2f})" if ratio is not None else "")
            return s * mult, {"sigma_daily": float(by_L[0]), "sigma_method": "backtest", "sigma_note": note,
                              "shift_flag": flag, "shift_ratio": ratio}
        sd = float(r.get("sigma_daily") or 0.0)
        return sd * math.sqrt(L), {"sigma_daily": sd, "sigma_method": "menu_sum",
                                   "sigma_note": f"메뉴 σ × BOM 단순 합 {sd:.1f}/일 × √{L}", "shift_flag": False,
                                   "shift_ratio": None}
    return fn


def recommend_orders(ds: Dataset, requirement_df: pd.DataFrame, projection: ProjectionResult, params: EngineParams,
                     sigma_df: Optional[pd.DataFrame] = None, plans: Optional[dict[str, Any]] = None,
                     notes: Optional[list[str]] = None) -> OrderResult:
    """매장×공급사별 발주 추천.

    sigma_df: requirement.ingredient_sigma_backtest 결과(sigma_by_L, shift_flag) 또는 ingredient_sigma 결과(sigma_daily). 없으면 σ=0.
    plans   : supplier_plans 결과 (없으면 여기서 계산)
    """
    plans = supplier_plans(ds, params, notes) if plans is None else plans
    cal = plans.get("_calendar")
    z = z_value(params.service_level)
    sig_fn = _sigma_lookup(sigma_df, params)
    summ = projection.summary
    sup = ds.get("05_suppliers")
    sup_names = dict(zip(sup["supplier_id"], sup.get("supplier_name", sup["supplier_id"]))) if sup is not None else {}
    raw = []
    for _, row in summ.iterrows():
        sid = row.get("supplier_id")
        plan = plans.get(sid)
        if plan is None or sid == "_calendar":
            continue
        raw.append(_line_for(ds, params, projection, plan, sup_names.get(sid, sid), row,
                             sig_fn, z))
    if not raw:
        return OrderResult(pd.DataFrame(), pd.DataFrame(), plans)
    lines = pd.DataFrame(raw)
    lines = prep.add_order_units(ds, lines, "net_need_use")
    lines["raw_order_units"] = lines["net_need_use"] / lines["use_qty_per_order_unit"]
    lines["ceil_order_units"] = np.ceil(np.round(lines["raw_order_units"], 9)).astype(int)
    lines["order_date"] = lines["order_deadline"].dt.normalize()
    priced = []
    for od, part in lines.groupby("order_date"):
        try:
            pt = prep.price_table_asof(ds, od)[["ingredient_id", "unit_price", "valid_from"]].rename(
                columns={"valid_from": "price_valid_from"})
            priced.append(part.merge(pt, on="ingredient_id", how="left", validate="many_to_one"))
        except Exception:  # 단가 이력 테이블이 없으면 금액 없이 수량만
            priced.append(part.assign(unit_price=np.nan, price_valid_from=pd.NaT))
    lines = pd.concat(priced, ignore_index=True)
    if lines["unit_price"].isna().any() and notes is not None:
        notes.append("단가 이력이 없는 식자재는 금액을 0 으로 합산 (발주서 합계 과소 가능)")
    lines["amount"] = lines["order_qty"] * lines["unit_price"]
    out_rows, explains = [], []
    for e in lines.to_dict("records"):
        ev = evaluate_order_qty(projection, e, int(e["order_qty"]), params.expiry_alert_days)
        e.update({k: ev[k] for k in ("consumable_capacity", "waste_risk", "waste_risk_qty", "stockout_date_after",
                                     "covered_until_arrival2", "waste_after_qty")})
        base_waste = float(summ.loc[(summ["store_id"] == e["store_id"]) & (summ["ingredient_id"] == e["ingredient_id"]),
                                    "waste_qty"].sum())
        e["new_lot_waste_qty"] = max(0.0, ev["waste_after_qty"] - base_waste)
        e["explain"] = {k: e.get(k) for k in EXPLAIN_KEYS}
        out_rows.append(e)
    lines = pd.DataFrame(out_rows)
    pos = _po_table(lines, plans, cal, params)
    if notes is not None:
        method = str(lines["sigma_method"].iloc[0]) if "sigma_method" in lines.columns and len(lines) else ""
        if method == "backtest":
            sigma_txt = (f"안전재고 = z({params.service_level:.0%}) {z:.2f} × σ(식자재, 커버 구간 합계); "
                         "σ = 같은 예측식을 과거에 매일 적용한 식자재 단위 잔차 RMSE(커버일수별), "
                         f"급변 품목(최근 2주/8주 소요 비율 밴드 밖)은 × {params.shift_sigma_mult:.1f}")
        else:
            sigma_txt = (f"안전재고 = z({params.service_level:.0%}) {z:.2f} × σ(식자재, 일) × √(커버 일수); "
                         "식자재 σ = 메뉴 σ × BOM 의 단순 합(보수적)")
        for t in (sigma_txt,
                  "가용재고 = 기준일 로트 잔량 − 다음 도착(arrival2) 전에 기한이 지나 버려질 양 (FEFO 전개로 계산)",
                  "도착 전 품절분은 이월되지 않는다고 보고 부족분 계산에서 뺌 (긴급발주·이동 대안으로 안내)",
                  "발주서 최소발주금액은 매장×공급사×발주일 합계로 판정"):
            if t not in notes:
                notes.append(t)
    return OrderResult(lines.sort_values(["store_id", "supplier_id", "ingredient_id"]).reset_index(drop=True), pos, plans)


def _po_table(lines: pd.DataFrame, plans: dict[str, Any], cal: Any, params: EngineParams) -> pd.DataFrame:
    """매장×공급사 발주서 요약."""
    rows = []
    for (sid, pid), part in lines.groupby(["store_id", "supplier_id"]):
        plan = plans[pid]
        moa = float(cal.terms(pid).min_order_amount) if cal is not None else 0.0
        total = float(part["amount"].fillna(0).sum())
        status, short = po_status(total, moa)
        if (part["order_qty"] > 0).any() and part.loc[part["order_qty"] > 0, "amount"].isna().any():
            status = "단가 없음(금액 일부 미계산)"
        rows.append({"store_id": sid, "supplier_id": pid, "supplier_name": part["supplier_name"].iloc[0],
                     "order_deadline": plan.order1_deadline, "arrival1": plan.arrival1, "arrival2": plan.arrival2,
                     "plan_text": plan.explain(), "n_lines": int((part["order_qty"] > 0).sum()),
                     "n_items": int(len(part)), "total_amount": total, "min_order_amount": moa,
                     "status": status, "shortfall": short,
                     "is_due_today": bool(plan.order1_deadline.normalize() == params.decision_datetime.normalize())})
    return pd.DataFrame(rows)


def _n(v: Any, digits: int = 1) -> str:
    """숫자 표시 (정수면 천 단위 쉼표, 아니면 소수 digits 자리)."""
    if v is None or (isinstance(v, float) and (math.isnan(v) or math.isinf(v))):
        return "-"
    v = float(v)
    return f"{v:,.0f}" if abs(v - round(v)) < 1e-9 else f"{v:,.{digits}f}"


def _d(t: Any) -> str:
    if t is None or pd.isna(t):
        return "-"
    t = pd.Timestamp(t)
    return f"{t:%m/%d}({WEEKDAY_KO[t.isoweekday() - 1]})"


def format_explain(e: dict[str, Any], store_name: Optional[str] = None) -> list[str]:
    """explain dict → 설명 패널 줄 목록 (design_notes 2.5 예시 형식). 숫자는 모두 dict 값 그대로."""
    u, ou = e.get("use_unit") or "", e.get("order_unit") or ""
    lines = [f"[{e.get('ingredient_name')} / {store_name or e.get('store_id')} / {e.get('supplier_name') or e.get('supplier_id')}]",
             f"발주 판단 시각 {pd.Timestamp(e['decision_datetime']):%m/%d %H:%M} 기준: {e.get('plan_text')}",
             f"커버해야 하는 기간: {_d(e['window_start'])} ~ {_d(e['window_end'])} ({e['window_days']}일, 다음 도착 전날까지)"]
    daily = ", ".join(_n(v) for _, v in (e.get("daily_requirement") or []))
    lines.append(f"  예측 소요            {_n(e['demand_window'])} {u}  (일별: {daily})")
    if e.get("unmet_before_arrival1", 0) > 1e-9:
        lines.append(f"− 도착 전 품절분        {_n(e['unmet_before_arrival1'])} {u}  ({_d(e['arrival1'])} 도착 전 부족 → "
                     "이번 발주로 못 막음, 긴급발주·이동 검토)")
    if e.get("sigma_method") == "backtest":
        lines.append(f"+ 안전재고 ({e['service_level']:.0%})      {_n(e['safety_stock'])} {u}  "
                     f"(σ({e['window_days']}일 합계)={_n(e.get('sigma_window'))}{u} × z {e['z']:.2f}; "
                     f"σ 출처: {e.get('sigma_note')}) [가정: 서비스 수준]")
    else:
        lines.append(f"+ 안전재고 ({e['service_level']:.0%})      {_n(e['safety_stock'])} {u}  "
                     f"(σ={_n(e['sigma_daily'])}{u}/일 × √{e['window_days']} × {e['z']:.2f}) [가정: 서비스 수준]")
    exp_txt = ""
    if e.get("expiring_excluded", 0) > 1e-9:
        lots = ", ".join(f"{_d(x['last_use_date'])} 만료 {_n(x['wasted'])}" for x in e.get("expiring_lots") or [])
        exp_txt = f", 그중 {_n(e['expiring_excluded'])}{u}는 기한 내 못 써서 제외: {lots}"
    lines.append(f"− 가용재고             {_n(e['available'])} {u}  (현재고 {_n(e['on_hand'])}{u}, 로트 {e['n_lots']}개{exp_txt})")
    inb = "; ".join(f"{x['po_line_id']} {_d(x['arrival_date'])} 도착 {_n(x['qty'])}" for x in e.get("inbound_lines") or [])
    lines.append(f"− 입고 예정             {_n(e['inbound'])} {u}  ({inb or '없음'})")
    lines.append(f"= 부족분               {_n(e['net_need_use'])} {u}")
    conv = e.get("use_qty_per_order_unit")
    if e.get("net_need_use", 0) > 1e-9:
        lines.append(f"→ 발주단위 환산         {_n(e['net_need_use'])} / {_n(conv)}{u}/{ou} = {e['raw_order_units']:.2f} "
                     f"→ 올림 {e['ceil_order_units']}{ou}")
        moq = e.get("min_order_qty")
        moq_txt = f"최소발주수량 {_n(moq)}{ou} {'충족' if e['ceil_order_units'] >= (moq or 0) else '→ 올림 적용'}"
        price = e.get("unit_price")
        lines.append(f"→ {moq_txt}. 발주 {e['order_qty']}{ou} = {_n(e['order_use_qty'])}{u}, "
                     f"단가 {_n(price)}원/{ou} ({_d(e.get('price_valid_from'))}부터) → {_n(e['amount'])}원")
    else:
        lines.append("→ 이번 발주 불필요 (0)")
    shelf = e.get("shelf_life_days")
    ext = f", 예측 기간 밖 {e['shelf_extrapolated_days']}일은 일평균으로 외삽" if e.get("shelf_extrapolated_days") else ""
    if e.get("order_use_qty", 0) > 0:
        risk = "폐기 위험" if e.get("waste_risk") else "낮음"
        lines.append(f"폐기 위험: {risk} (도착 후 사용기한 {_n(shelf)}일 동안 예상 소요 {_n(e['shelf_requirement'])}{u}{ext}, "
                     f"도착 시점 재고 {_n(e['stock_at_arrival1'])}{u} → 소진 가능 {_n(e['consumable_capacity'])}{u}, "
                     f"발주 {_n(e['order_use_qty'])}{u}"
                     + (f", 초과 {_n(e['waste_risk_qty'])}{u})" if e.get("waste_risk") else ")"))
    so_b, so_a = e.get("stockout_date_base"), e.get("stockout_date_after")
    has = lambda t: t is not None and pd.notna(t)  # noqa: E731
    after = (f"도착({_d(e['arrival1'])})부터 다음 도착 전날까지 품절 없음" if e.get("covered_until_arrival2")
             else f"도착 후 {_d(so_a)} 품절 예상 (커버 구간 안)")
    tail = f"; 그다음 발주가 없다고 보면 {_d(so_a)} 품절" if e.get("covered_until_arrival2") and has(so_a) else ""
    lines.append(f"재고 전개: 이번 발주 없으면 {_d(so_b) + ' 품절 예상' if has(so_b) else '기간 내 품절 없음'}, "
                 f"이번 발주 반영 시 {after}{tail} · 기간 내 예상 폐기 {_n(e.get('waste_after_qty'))}{u}")
    return lines
