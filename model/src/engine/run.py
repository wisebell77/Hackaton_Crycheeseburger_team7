"""
엔진 한 번 실행: 예측 → 소요량 → FEFO 전개 → 발주 추천 → 알림. UI·LLM 은 EngineOutput 만 본다.

    from src.loader import load_all
    from src.engine import EngineParams, run_engine, briefing_template
    out = run_engine(load_all("data/fixture"), EngineParams())
    print(briefing_template(out))
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Optional

import numpy as np
import pandas as pd

from .. import prep
from ..loader import Dataset
from .alerts import build_alerts
from .forecast import forecast_menu_daily
from .order import OrderResult, recommend_orders, supplier_plans
from .params import ASSUMPTION_PARAMS, EngineParams, resolve_params
from .projection import ProjectionResult, ingredient_master, project_inventory
from .requirement import ingredient_requirement, ingredient_sigma, ingredient_sigma_backtest

WEEKDAY_KO = "월화수목금토일"


@dataclass
class EngineOutput:
    """엔진 결과 묶음 (모든 숫자는 결정론적 코드에서 나옴)."""

    params: EngineParams
    resolve_notes: dict[str, Any]
    forecast: pd.DataFrame
    basis: pd.DataFrame
    requirement: pd.DataFrame
    sigma: pd.DataFrame
    projection: ProjectionResult
    orders: OrderResult
    alerts: pd.DataFrame
    assumptions: list[str]
    ingredients: pd.DataFrame
    stores: pd.DataFrame
    timings: dict[str, float] = field(default_factory=dict)

    @property
    def asof(self) -> pd.Timestamp:
        return self.params.asof

    @property
    def decision_datetime(self) -> pd.Timestamp:
        return self.params.decision_datetime


def _stores(ds: Dataset) -> pd.DataFrame:
    st = ds.get("01_stores")
    if st is None:
        return pd.DataFrame(columns=["store_id", "store_name"])
    out = st.copy()
    if "store_name" not in out.columns:
        out["store_name"] = out["store_id"]
    return out


def _param_assumptions(params: EngineParams, notes: dict[str, Any]) -> list[str]:
    """이번 실행의 파라미터 중 가정값 설명."""
    out = [f"기준일 {params.asof:%Y-%m-%d} 영업 종료 후 재고에서 출발, 발주 판단 시각 {params.decision_datetime:%m/%d %H:%M} "
           f"({notes.get('asof_source', '')})",
           f"안전재고 서비스 수준 {params.service_level:.0%} (가정, 사용자 선택)",
           f"긴급발주: 익일 도착 · 단가 할증 {params.emergency_markup:.0%} (가정, 데이터에 없음)",
           f"매장 간 이동 비용 {params.transfer_cost:,.0f}원/건, 당일 이동 가능 (가정)",
           f"사용기한 임박 기준 {params.expiry_alert_days}일 (가정)",
           f"과잉 기준 커버일수 {params.overstock_cover_days:.1f}일 — {notes.get('overstock_source', '')}"]
    if abs(params.demand_multiplier - 1.0) > 1e-9:
        out.append(f"수요 시나리오 {params.demand_multiplier - 1:+.0%} 를 모든 메뉴 예측에 곱함 (사용자 입력)")
    if params.menu_multipliers:
        out.append("메뉴별 시나리오 배율: " + ", ".join(f"{k}×{v:g}" for k, v in params.menu_multipliers.items()))
    if params.delay_buffer_days:
        out.append(f"입고 예정 PO 도착을 {params.delay_buffer_days}일 늦게 봄 (입고 지연 보정, 사용자 입력)")
    if not params.apply_promo:
        out.append("프로모션 계수 미적용 (사용자 선택)")
    if not params.apply_holiday:
        out.append("공휴일 계수 미적용 (사용자 선택)")
    out.append("예측 소요 = demand_qty(품절 전 수요) × BOM — 취소분도 소요로 봄(보수적), 세트는 세트 자체 BOM")
    return out


def run_engine(ds: Dataset, params: Optional[EngineParams] = None, daily: Optional[pd.DataFrame] = None,
               forecaster: Optional[Callable[..., tuple[pd.DataFrame, pd.DataFrame]]] = None) -> EngineOutput:
    """엔진 전체 실행. daily 에 prep.daily_sales(ds) 결과를 넘기면 재사용한다 (앱 캐시용).

    forecaster: 예측 함수 교체용. forecast_menu_daily 와 같은 시그니처 (ds, params, daily=, notes=) 로
    (forecast_df[date, store_id, menu_id, forecast_qty, is_available], basis_df[store_id, menu_id, sigma, ...]) 를 돌려준다.
    """
    t0 = time.perf_counter()
    timings: dict[str, float] = {}
    notes: list[str] = []
    params, rnotes = resolve_params(ds, params or EngineParams())
    plans = supplier_plans(ds, params, notes)
    arr2 = [p.arrival2 for k, p in plans.items() if k != "_calendar"]
    if arr2:
        need = int((max(arr2) - params.asof).days)
        if need > params.horizon_days:
            notes.append(f"다음 도착일까지 덮도록 예측 기간을 {params.horizon_days}일 → {need}일로 늘림")
            params = replace(params, horizon_days=need)
    daily = prep.daily_sales(ds) if daily is None else daily
    timings["daily_sales"] = time.perf_counter() - t0
    forecast, basis = (forecaster or forecast_menu_daily)(ds, params, daily=daily, notes=notes)
    timings["forecast"] = time.perf_counter() - t0
    req = ingredient_requirement(forecast, ds)
    sigma = None
    if str(params.sigma_method) == "backtest":
        try:
            sigma = ingredient_sigma_backtest(ds, daily, params, notes)
        except Exception as e:  # 백테스트 σ가 실데이터에서 깨져도 엔진은 메뉴 합산 σ로 계속
            notes.append(f"식자재 단위 백테스트 σ 계산 실패({type(e).__name__}: {e}) → 메뉴 σ 합산으로 대체")
            sigma = None
    if sigma is None or len(sigma) == 0:
        sigma = ingredient_sigma(basis, ds)
        if "식자재 σ = 메뉴 σ × BOM 단순 합 (보수적, 백테스트 σ 대체)" not in notes:
            notes.append("식자재 σ = 메뉴 σ × BOM 단순 합 (보수적, 백테스트 σ 대체)")
    proj = project_inventory(ds, req, params, notes)
    timings["projection"] = time.perf_counter() - t0
    orders = recommend_orders(ds, req, proj, params, sigma, plans, notes)
    timings["orders"] = time.perf_counter() - t0
    alerts = build_alerts(proj, orders, params)
    assumptions = _param_assumptions(params, rnotes) + [n for n in notes]
    timings["total"] = time.perf_counter() - t0
    return EngineOutput(params, rnotes, forecast, basis, req, sigma, proj, orders, alerts, assumptions,
                        ingredient_master(ds), _stores(ds), timings)


# ---------------------------------------------------------------------------
# 요약 (규칙 기반 브리핑 / LLM 입력 JSON)
# ---------------------------------------------------------------------------

def _f(v: Any) -> str:
    if v is None or (isinstance(v, float) and (math.isnan(v) or math.isinf(v))):
        return "-"
    return f"{float(v):,.0f}"


def _d(t: Any) -> str:
    if t is None or pd.isna(t):
        return "-"
    t = pd.Timestamp(t)
    return f"{t:%m/%d}({WEEKDAY_KO[t.isoweekday() - 1]})"


def _by_store(df: pd.DataFrame, store_id: Optional[str]) -> pd.DataFrame:
    if df is None or df.empty or store_id in (None, "", "전체") or "store_id" not in df.columns:
        return df
    return df[df["store_id"] == store_id]


def kpis(out: EngineOutput, store_id: Optional[str] = None) -> dict[str, Any]:
    """홈 KPI: 오늘 마감 발주서 수·총액, 알림 유형별 건수."""
    pos = _by_store(out.orders.pos, store_id)
    due = pos[(pos["is_due_today"]) & (pos["total_amount"] > 0)] if len(pos) else pos
    al = _by_store(out.alerts, store_id)
    cnt = al["type"].value_counts().to_dict() if len(al) else {}
    return {"due_po_count": int(len(due)), "due_supplier_count": int(due["supplier_id"].nunique()) if len(due) else 0,
            "due_total": float(due["total_amount"].sum()) if len(due) else 0.0,
            "all_po_total": float(pos["total_amount"].sum()) if len(pos) else 0.0,
            "n_stockout": int(cnt.get("품절위험", 0)), "n_expiring": int(cnt.get("사용기한임박", 0)),
            "n_overstock": int(cnt.get("과잉", 0)),
            "n_below_min": int((pos["status"] == "미달").sum()) if len(pos) else 0}


def summary_json(out: EngineOutput, store_id: Optional[str] = None, top_n: int = 8) -> dict[str, Any]:
    """LLM 브리핑 입력용 집계 JSON (원본 데이터 없이 엔진 결과만, 숫자는 반올림)."""
    k = kpis(out, store_id)
    pos = _by_store(out.orders.pos, store_id)
    lines = _by_store(out.orders.lines, store_id)
    al = _by_store(out.alerts, store_id)
    names = dict(zip(out.stores["store_id"], out.stores["store_name"]))
    po_list = [{"매장": names.get(r.store_id, r.store_id), "공급사": r.supplier_name, "마감": _d(r.order_deadline),
                "오늘마감": bool(r.is_due_today), "도착": _d(r.arrival1), "총액_원": round(r.total_amount),
                "최소발주금액_원": round(r.min_order_amount), "상태": r.status, "부족금액_원": round(r.shortfall)}
               for r in pos.itertuples(index=False)] if len(pos) else []
    alert_list = [{"매장": names.get(r.store_id, r.store_id), "식자재": r.ingredient_name, "유형": r.type,
                   "심각도": r.severity, "수량": round(float(r.value_qty or 0), 1), "단위": r.use_unit,
                   "금액_원": None if pd.isna(r.value_won) else round(float(r.value_won)), "이유": r.reason,
                   "권고": r.action} for r in al.head(top_n).itertuples(index=False)] if len(al) else []
    risk = lines[lines["waste_risk"] == True] if len(lines) else lines  # noqa: E712
    risk_list = [{"매장": names.get(r.store_id, r.store_id), "식자재": r.ingredient_name,
                  "발주_사용단위": round(r.order_use_qty), "소진가능_사용단위": round(r.consumable_capacity),
                  "단위": r.use_unit} for r in risk.itertuples(index=False)] if len(risk) else []
    return {"기준일": f"{out.asof:%Y-%m-%d}", "발주판단시각": f"{out.decision_datetime:%Y-%m-%d %H:%M}",
            "매장": "전체" if store_id in (None, "", "전체") else names.get(store_id, store_id),
            "오늘마감_발주서수": k["due_po_count"], "오늘마감_총액_원": round(k["due_total"]),
            "발주필요_품목수": int((lines["order_qty"] > 0).sum()) if len(lines) else 0,
            "품절위험_건수": k["n_stockout"], "임박_건수": k["n_expiring"], "과잉_건수": k["n_overstock"],
            "발주서": po_list, "주요알림": alert_list, "폐기위험_발주": risk_list,
            "가정": out.assumptions[:6]}


def briefing_template(out: EngineOutput, store_id: Optional[str] = None) -> str:
    """규칙 기반 3~5문장 한국어 요약 (LLM 실패 시 폴백). 숫자는 모두 엔진 결과."""
    k = kpis(out, store_id)
    pos = _by_store(out.orders.pos, store_id)
    lines = _by_store(out.orders.lines, store_id)
    al = _by_store(out.alerts, store_id)
    names = dict(zip(out.stores["store_id"], out.stores["store_name"]))
    sents = []
    n_items = int((lines["order_qty"] > 0).sum()) if len(lines) else 0
    if k["due_po_count"]:
        due = pos[pos["is_due_today"] & (pos["total_amount"] > 0)]
        sups = ", ".join(sorted(due["supplier_name"].unique()))
        sents.append(f"{_d(out.asof)} 영업 종료 기준, 오늘 {_d(out.decision_datetime)} 마감인 발주서 {k['due_po_count']}건"
                     f"(공급사 {sups}), 총 {_f(k['due_total'])}원입니다. 전체 발주 필요 품목은 {n_items}개입니다.")
    else:
        nxt = pos[pos["total_amount"] > 0].sort_values("order_deadline") if len(pos) else pos
        tail = (f" 다음 마감은 {_d(nxt.iloc[0]['order_deadline'])} {nxt.iloc[0]['supplier_name']} "
                f"{_f(nxt.iloc[0]['total_amount'])}원입니다.") if len(nxt) else ""
        sents.append(f"{_d(out.asof)} 영업 종료 기준, 오늘 마감인 발주는 없습니다.{tail}")
    so = al[al["type"] == "품절위험"] if len(al) else al
    high = so[so["severity"] == "높음"] if len(so) else so
    mid = so[so["severity"] != "높음"] if len(so) else so
    if len(high):
        items = "; ".join(f"{names.get(r.store_id, r.store_id)} {r.ingredient_name}({r.reason})"
                          for r in high.head(2).itertuples(index=False))
        sents.append(f"이번 발주 도착 전 품절 위험 {len(high)}건 중 가장 급한 것은 {items}이며, 긴급발주·매장 간 이동을 검토하세요.")
    elif len(mid):
        items = ", ".join(f"{names.get(r.store_id, r.store_id)} {r.ingredient_name}" for r in mid.head(2).itertuples(index=False))
        sents.append(f"이번 발주 도착 전 품절 위험은 없지만 입고 예정 PO 에 기대는 품목 {len(mid)}건({items})은 "
                     "PO 가 제때 와야 하므로 도착을 확인하세요.")
    else:
        sents.append("이번 발주 도착 전 품절 위험 품목은 없습니다.")
    ex = al[al["type"] == "사용기한임박"] if len(al) else al
    risk = lines[lines["waste_risk"] == True] if len(lines) else lines  # noqa: E712
    if len(ex) or len(risk):
        parts = []
        if len(ex):
            r = ex.iloc[0]
            parts.append(f"사용기한 임박 {len(ex)}건(최대 {names.get(r.store_id, r.store_id)} {r.ingredient_name} "
                         f"{_f(r.value_qty)}{r.use_unit} 소진 불가 예상)")
        if len(risk):
            parts.append(f"발주량이 사용기한 내 소진 가능량을 넘는 품목 {len(risk)}개")
        sents.append("주의: " + ", ".join(parts) + " — 선사용 또는 발주량 조정을 권합니다.")
    below = pos[pos["status"] == "미달"] if len(pos) else pos
    if len(below):
        r = below.iloc[0]
        sents.append(f"{names.get(r.store_id, r.store_id)} {r.supplier_name} 발주서는 최소발주금액에 {_f(r.shortfall)}원 미달합니다.")
    sents.append(f"서비스 수준 {out.params.service_level:.0%}, 긴급 할증 {out.params.emergency_markup:.0%} 등은 가정값이므로 "
                 "'가정·데이터' 탭에서 확인하세요.")
    return " ".join(sents[:5]) if len(sents) <= 5 else " ".join(sents[:4] + sents[-1:])
