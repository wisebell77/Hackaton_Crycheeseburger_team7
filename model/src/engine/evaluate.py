"""
발주안 평가 엔진 — "이 발주안을 실행하면 무슨 일이 생기나" (2026-10-31, 해커톤 계획 (2)).

입력: 발주안 = 발주 라인 목록 DataFrame
      store_id, ingredient_id, order_qty(발주단위, 정수) [, order_date | arrival_date, order_type(정기/긴급), note]
      - arrival_date 가 없으면: 긴급은 order_date 다음 날(요일 무관, 명세: 다음날 07시 입고), 정기는 공급사 달력으로
        order_date(없으면 판단 시각 이후 첫 발주 기회)의 도착일을 구한다.
출력 (evaluate_plan → PlanEvaluation)
      lines    : 매장×식자재 1행. 평가 창(이 발주가 책임지는 기간) 안의 품절과 기간 전체의 폐기 — 기대 경로 + 시나리오 분포
      daily    : 기대 경로 일별 전개 (수요, 기말재고, 부족, 폐기, p90 부족, p10 재고) — 그래프용
      totals   : 발주 금액, 기대 품절 손실 매출, 기대 폐기 비용, 기대 총손실, p90 총손실, 품절 확률 (전체·매장별)
      scenarios: 시나리오별 전체 손실 (분포 그래프용)

평가 창 (window)
      이번 발주가 책임지는 기간 = 내일부터 '그 다음 발주분 도착 전날'(arrival2 − 1)까지 (공급사 달력, 발주 추천과 같은 구간).
      그 뒤의 품절은 다음 발주 결정의 몫이므로 이 발주안의 손실로 세지 않는다. 폐기는 기간(horizon) 전체를 본다 —
      이 발주로 들어온 로트가 사용기한 안에 다 소진되는지는 창을 넘어서야 알 수 있기 때문 (이후 추가 발주는 없다고 봄;
      FEFO 라 나중 로트가 먼저 들어온 로트의 소진을 막지 않으므로 신규 로트 폐기 추정에는 영향이 작다).
      window="horizon" 으로 바꾸면 "이 발주 말고는 아무것도 안 시켰을 때" 를 본다.

변동성 = 과거 잔차 시나리오 (historical simulation)
      같은 예측식을 과거에 매일 적용했을 때의 식자재 단위 '실제 ÷ 예측' 비율을 H일 블록으로 잘라 시나리오로 쓴다.
      시나리오 n (과거 시작일 t_n): 미래 h일째 수요 = 예측[h] × 비율[t_n + h]. 같은 t_n 을 모든 매장·식자재에 적용해
      '바쁜 날은 다 바쁘다'는 동시성을 보존한다. 분포 가정이 없고 급변 구간이 과거 빈도대로 들어간다.
      기대 경로 = 비율 1 (점 예측 그대로).

손실 환산
      품절 손실 매출: 메뉴 단위. 어떤 날 메뉴 m 의 판매 손실 비율 = m 의 재료 중 가장 모자란 재료의 부족 비율(부족/소요).
        손실 매출 = Σ_m 메뉴가격 × 예측 판매 × 손실 비율. 식자재별로 따로 매기면 같은 메뉴가 여러 번 세어지므로 그렇게 하지 않는다.
        식자재별 귀속은 그 메뉴에서 가장 모자란(binding) 재료에 준다.
      폐기 비용 = 폐기 수량 × 기준일 단가(발주단위 단가 ÷ 발주단위당 사용량). 기존 재고 폐기와 이번 발주 로트 폐기를 나눠 보여준다.
      발주 금액 = 발주 수량 × 발주일 기준 단가 (손실이 아니라 지출로 따로 보고)

하루 처리 순서 (명세 6절, projection.py 와 동일): 07시 입고 → 마지막 사용일이 지난 로트 폐기 → 소요 FEFO 차감 → 기말.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Union

import numpy as np
import pandas as pd

from .. import prep
from ..loader import Dataset, require
from .order import supplier_plans
from .params import EngineParams
from .projection import LOT_COLS, inbound_lots, initial_lots
from .requirement import BacktestArrays, backtest_ingredient_arrays

RATIO_CLIP = (0.0, 3.0)
FAR = 10 ** 6


# ---------------------------------------------------------------------------
# 발주안 만들기·정규화
# ---------------------------------------------------------------------------

def zero_plan() -> pd.DataFrame:
    """발주 없음 (기준선)."""
    return pd.DataFrame(columns=["store_id", "ingredient_id", "order_qty", "order_date", "arrival_date", "order_type", "note"])


def plan_from_orders(order_lines: pd.DataFrame, qty_col: str = "order_qty") -> pd.DataFrame:
    """엔진 발주 추천(orders.lines) → 발주안 (order_qty > 0 인 라인만)."""
    if order_lines is None or len(order_lines) == 0 or qty_col not in order_lines.columns:
        return zero_plan()
    df = order_lines[order_lines[qty_col] > 0]
    return pd.DataFrame({
        "store_id": df["store_id"].values, "ingredient_id": df["ingredient_id"].values,
        "order_qty": df[qty_col].astype(int).values,
        "order_date": pd.to_datetime(df["order_deadline"]).dt.normalize().values if "order_deadline" in df.columns else pd.NaT,
        "arrival_date": pd.to_datetime(df["arrival1"]).values if "arrival1" in df.columns else pd.NaT,
        "order_type": "정기", "note": "엔진 추천",
    }).reset_index(drop=True)


def scale_plan(plan: pd.DataFrame, factor: float, note: Optional[str] = None) -> pd.DataFrame:
    """발주 수량을 factor 배 (발주단위 올림). 0 은 0 그대로."""
    out = plan.copy()
    out["order_qty"] = np.ceil(out["order_qty"].astype(float) * float(factor) - 1e-9).astype(int)
    if note:
        out["note"] = note
    return out


def normalize_plan(ds: Dataset, plan: pd.DataFrame, params: EngineParams,
                   cal: Optional[prep.SupplierCalendar] = None, notes: Optional[list[str]] = None) -> pd.DataFrame:
    """발주안에 공급사·도착일·사용단위 수량·마지막 사용일·단가·금액을 붙인다.

    반환 컬럼: store_id, ingredient_id, ingredient_name, supplier_id, order_type, order_qty, order_unit, use_qty_per_order_unit,
              use_qty, order_date, arrival_date, last_use_date, unit_price, amount, note, arrival_source
    """
    cols = ["store_id", "ingredient_id", "ingredient_name", "supplier_id", "order_type", "order_qty", "order_unit",
            "use_qty_per_order_unit", "use_qty", "order_date", "arrival_date", "last_use_date", "unit_price", "amount",
            "note", "arrival_source"]
    if plan is None or len(plan) == 0:
        return pd.DataFrame(columns=cols)
    ing = require(ds, "03_ingredients", ["ingredient_id"])
    icols = [c for c in ("ingredient_id", "ingredient_name", "supplier_id", "order_unit", "use_qty_per_order_unit",
                         "shelf_life_days") if c in ing.columns]
    p = plan.copy()
    for c in ("order_date", "arrival_date", "order_type", "note"):
        if c not in p.columns:
            p[c] = None
    p["order_qty"] = pd.to_numeric(p["order_qty"], errors="coerce").fillna(0).astype(int)
    p = p.merge(ing[icols].drop_duplicates("ingredient_id"), on="ingredient_id", how="left", validate="many_to_one")
    p["order_type"] = p["order_type"].where(p["order_type"].notna(), "정기")
    cal = cal or prep.SupplierCalendar.from_dataset(ds)
    decision = params.decision_datetime
    order_dates, arrivals, sources = [], [], []
    for r in p.itertuples(index=False):
        od = pd.Timestamp(r.order_date).normalize() if r.order_date is not None and pd.notna(r.order_date) else None
        ad = pd.Timestamp(r.arrival_date).normalize() if r.arrival_date is not None and pd.notna(r.arrival_date) else None
        if ad is not None:
            sources.append("입력값")
            od = od or (ad - pd.Timedelta(days=1))
        elif str(r.order_type).startswith("긴급"):
            od = od or decision.normalize()
            ad = od + pd.Timedelta(days=1)
            sources.append("긴급: 발주 다음 날 (요일 무관)")
        else:
            try:
                if od is None:
                    od = pd.Timestamp(cal.next_order_datetime(r.supplier_id, decision)).normalize()
                ad = pd.Timestamp(cal.arrival_date(r.supplier_id, od)).normalize()
                sources.append("공급사 달력")
            except Exception:
                od = od or decision.normalize()
                ad = od + pd.Timedelta(days=1)
                sources.append("달력 없음 → 다음 날 (가정)")
        order_dates.append(od); arrivals.append(ad)
    p["order_date"] = pd.to_datetime(order_dates); p["arrival_date"] = pd.to_datetime(arrivals); p["arrival_source"] = sources
    shelf = p["shelf_life_days"] if "shelf_life_days" in p.columns else pd.Series(np.nan, index=p.index)
    p["last_use_date"] = [a + pd.Timedelta(days=int(s) - 1) if pd.notna(s) and s else pd.Timestamp("2099-12-31")
                          for a, s in zip(p["arrival_date"], shelf)]
    p["use_qty"] = p["order_qty"] * p["use_qty_per_order_unit"].astype(float)
    parts = []
    for od, part in p.groupby("order_date"):
        try:
            pt = prep.price_table_asof(ds, od)[["ingredient_id", "unit_price"]]
            parts.append(part.merge(pt, on="ingredient_id", how="left", validate="many_to_one"))
        except Exception:
            parts.append(part.assign(unit_price=np.nan))
    p = pd.concat(parts, ignore_index=True) if parts else p.assign(unit_price=np.nan)
    p["amount"] = p["order_qty"] * p["unit_price"]
    if notes is not None and (p["unit_price"].isna() & (p["order_qty"] > 0)).any():
        notes.append("단가 이력이 없는 발주 라인은 금액을 계산하지 못함")
    if "ingredient_name" not in p.columns:
        p["ingredient_name"] = p["ingredient_id"]
    return p[cols].reset_index(drop=True)


# ---------------------------------------------------------------------------
# 시나리오 (과거 잔차 블록)
# ---------------------------------------------------------------------------

@dataclass
class ScenarioSet:
    """미래 H일 수요 비율 시나리오. ratios[n, s, i, h] = 과거 블록의 실제/예측 비율."""
    ratios: np.ndarray
    stores: list[str]
    ingredients: list[str]
    start_dates: list[pd.Timestamp]
    horizon: int
    source: str = "historical"

    @property
    def n(self) -> int:
        return int(self.ratios.shape[0])


def historical_scenarios(bt: BacktestArrays, horizon: int, max_scenarios: Optional[int] = None,
                         seed: int = 0) -> ScenarioSet:
    """백테스트 배열에서 H일 블록 비율 시나리오를 만든다 (평가 구간의 가능한 모든 시작일, 많으면 균등 표본)."""
    fc, act = bt.fc_ing, bt.act_ing
    T = fc.shape[2]
    with np.errstate(divide="ignore", invalid="ignore"):
        ratio = np.where(fc > 1e-9, act / np.where(fc > 1e-9, fc, 1.0), 1.0)
    ratio = np.clip(ratio, *RATIO_CLIP)
    starts = np.arange(bt.eval_start, T - horizon + 1)
    if len(starts) == 0:
        starts = np.arange(bt.eval_start, max(bt.eval_start + 1, T - 1))
    if max_scenarios is not None and len(starts) > max_scenarios:
        rng = np.random.default_rng(seed)
        starts = np.sort(rng.choice(starts, size=max_scenarios, replace=False))
    blocks = []
    span = max(T - bt.eval_start, 1)
    for t in starts:
        idx = np.arange(t, t + horizon)
        idx = np.where(idx < T, idx, bt.eval_start + (idx - T) % span)
        blocks.append(ratio[:, :, idx])
    ratios = np.stack(blocks, axis=0) if blocks else np.ones((1, fc.shape[0], fc.shape[1], horizon))
    return ScenarioSet(ratios, list(bt.stores), list(bt.ingredients), [bt.dates[t] for t in starts], horizon)


# ---------------------------------------------------------------------------
# FEFO 시뮬레이션 (시나리오 벡터화)
# ---------------------------------------------------------------------------

def _simulate_lots(qty0: np.ndarray, arrival_idx: np.ndarray, expire_start_idx: np.ndarray, is_new: np.ndarray,
                   demand: np.ndarray) -> dict[str, np.ndarray]:
    """로트 FEFO 전개. demand[N, H]. 로트는 (마지막 사용일, 도착) 오름차순 정렬돼 있어야 한다.

    하루 h: 도착(arrival_idx ≤ h) → 폐기(expire_start_idx ≤ h, 즉 마지막 사용일 < 오늘) → FEFO 차감 → 기말.
    반환: shortage, waste_exist, waste_new, on_hand_end, consumed (각 [N, H])
    """
    N, H = demand.shape
    L = len(qty0)
    qty = np.tile(qty0.astype(float), (N, 1))
    shortage = np.zeros((N, H)); w_ex = np.zeros((N, H)); w_new = np.zeros((N, H)); end = np.zeros((N, H)); cons = np.zeros((N, H))
    for h in range(H):
        for l in range(L):
            if arrival_idx[l] <= h and expire_start_idx[l] <= h:
                if is_new[l]:
                    w_new[:, h] += qty[:, l]
                else:
                    w_ex[:, h] += qty[:, l]
                qty[:, l] = 0.0
        rem = demand[:, h].astype(float).copy()
        for l in range(L):
            if arrival_idx[l] > h or expire_start_idx[l] <= h:
                continue
            take = np.minimum(qty[:, l], rem)
            qty[:, l] -= take; rem -= take
        shortage[:, h] = rem
        cons[:, h] = demand[:, h] - rem
        avail = arrival_idx <= h
        end[:, h] = qty[:, avail].sum(axis=1) if avail.any() else 0.0
    return {"shortage": shortage, "waste_exist": w_ex, "waste_new": w_new, "on_hand_end": end, "consumed": cons}


# ---------------------------------------------------------------------------
# 평가
# ---------------------------------------------------------------------------

@dataclass
class PlanEvaluation:
    name: str
    plan: pd.DataFrame
    lines: pd.DataFrame
    daily: pd.DataFrame
    totals: dict[str, Any]
    by_store: pd.DataFrame
    scenarios: pd.DataFrame
    assumptions: list[str] = field(default_factory=list)


def _pair_windows(ds: Dataset, params: EngineParams, dates: pd.DatetimeIndex, plan_n: pd.DataFrame,
                  window: Union[str, int], notes: list[str]) -> dict[tuple[str, str], tuple[int, str]]:
    """매장×식자재 → (평가 창 끝 인덱스(포함), 설명). window: 'cycle' | 'horizon' | 일수(int)."""
    H = len(dates)
    if window == "horizon":
        default = (H - 1, f"기간 전체 {dates[0]:%m/%d}~{dates[-1]:%m/%d}")
        return {}, default  # type: ignore[return-value]
    if isinstance(window, int):
        e = max(min(window, H) - 1, 0)
        return {}, (e, f"{window}일 {dates[0]:%m/%d}~{dates[e]:%m/%d}")  # type: ignore[return-value]
    out: dict[tuple[str, str], tuple[int, str]] = {}
    ing = ds.get("03_ingredients")
    sup_of = dict(zip(ing["ingredient_id"], ing["supplier_id"])) if ing is not None and "supplier_id" in ing.columns else {}
    try:
        plans = supplier_plans(ds, params, notes)
    except Exception:
        plans = {}
    default_end = (H - 1, f"공급사 달력 없음 → 기간 전체 {dates[0]:%m/%d}~{dates[-1]:%m/%d}")
    cycle_len: dict[str, int] = {}
    for sid, pl in plans.items():
        if sid == "_calendar" or pl is None:
            continue
        a1, a2 = pd.Timestamp(pl.arrival1), pd.Timestamp(pl.arrival2)
        cycle_len[sid] = max(int((a2 - a1).days), 1)
    extra = plan_n.groupby(["store_id", "ingredient_id"])["arrival_date"].max().to_dict() if len(plan_n) else {}
    for iid, sid in sup_of.items():
        pl = plans.get(sid)
        if pl is None:
            continue
        a2 = pd.Timestamp(pl.arrival2)
        end_date = a2 - pd.Timedelta(days=1)
        for s in (ds.get("01_stores")["store_id"] if ds.get("01_stores") is not None else []):
            e_date = end_date
            # 발주안에 더 늦은 도착이 있으면 (사용자 다중일 발주안) 그 도착 + 주기 − 1 까지
            late = extra.get((s, iid))
            if late is not None and pd.notna(late) and pd.Timestamp(late) > pd.Timestamp(pl.arrival1):
                e_date = max(e_date, pd.Timestamp(late) + pd.Timedelta(days=cycle_len.get(sid, 1) - 1))
            e = int(np.searchsorted(dates.values, np.datetime64(e_date), side="right") - 1)
            e = min(max(e, 0), H - 1)
            out[(s, iid)] = (e, f"{dates[0]:%m/%d}~{dates[e]:%m/%d} (다음 발주분 도착 {a2:%m/%d} 전날까지)")
    return out, default_end  # type: ignore[return-value]


def evaluate_plan(ds: Dataset, params: EngineParams, forecast_df: pd.DataFrame, requirement_df: pd.DataFrame,
                  plan: pd.DataFrame, scenarios: Optional[ScenarioSet] = None, name: str = "발주안",
                  lot_inputs: Optional[pd.DataFrame] = None, daily: Optional[pd.DataFrame] = None,
                  window: Union[str, int] = "cycle", notes: Optional[list[str]] = None) -> PlanEvaluation:
    """발주안 하나를 평가한다. params 는 resolve_params 를 거친 값, forecast/requirement 는 run_engine 결과를 그대로 넣는다.

    scenarios 가 None 이면 백테스트 배열에서 과거 잔차 시나리오를 만든다 (daily 를 주면 재사용).
    lot_inputs 가 None 이면 projection 과 같은 함수로 기준일 로트 + 미입고 PO 를 만든다.
    """
    asof = params.asof
    assumptions: list[str] = []
    dates = pd.DatetimeIndex(sorted(requirement_df["date"].unique()))
    H = len(dates); didx = {d: i for i, d in enumerate(dates)}
    pairs = requirement_df[["store_id", "ingredient_id"]].drop_duplicates().sort_values(["store_id", "ingredient_id"])
    req = requirement_df.pivot_table(index=["store_id", "ingredient_id"], columns="date", values="requirement_qty",
                                     aggfunc="sum").reindex(columns=dates).fillna(0.0)
    if scenarios is None:
        bt = backtest_ingredient_arrays(ds, prep.daily_sales(ds) if daily is None else daily, params)
        scenarios = historical_scenarios(bt, H) if bt is not None else None
    if scenarios is None:
        assumptions.append("이력이 짧아 과거 잔차 시나리오를 만들지 못함 → 기대 경로만 평가")
        scn_ids = None; N = 0
    else:
        scn_ids = ({s: k for k, s in enumerate(scenarios.stores)}, {i: k for k, i in enumerate(scenarios.ingredients)})
        N = scenarios.n
    plan_n = normalize_plan(ds, plan, params, notes=assumptions)
    if lot_inputs is None:
        init = initial_lots(ds, asof, None); inb = inbound_lots(ds, params, None)
        parts = [p[LOT_COLS] for p in (init, inb) if p is not None and len(p)]
        lot_in = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(columns=LOT_COLS)
    else:
        lot_in = lot_inputs
    windows, default_win = _pair_windows(ds, params, dates, plan_n, window, assumptions)
    try:
        pt = prep.price_table_asof(ds, asof)
        cost_pu = dict(zip(pt["ingredient_id"], pt["unit_price_per_use_unit"].astype(float)))
    except Exception:
        cost_pu = {}
    ing = ds.get("03_ingredients")
    names = dict(zip(ing["ingredient_id"], ing["ingredient_name"])) if ing is not None and "ingredient_name" in ing.columns else {}
    units = dict(zip(ing["ingredient_id"], ing["use_unit"])) if ing is not None and "use_unit" in ing.columns else {}
    bom = require(ds, "04_bom", ["menu_id", "ingredient_id", "qty_per_menu"])
    menus = ds.get("02_menus")
    price_m = dict(zip(menus["menu_id"], menus["virtual_price"].astype(float))) if menus is not None and "virtual_price" in menus.columns else {}
    fc_menu = forecast_df.pivot_table(index=["store_id", "menu_id"], columns="date", values="forecast_qty",
                                      aggfunc="sum").reindex(columns=dates).fillna(0.0)
    R = N + 1  # 0번 = 기대 경로

    line_rows, daily_rows = [], []
    store_lost = {}      # store → [R] 손실 매출
    store_attr = {}      # store → {ingredient: [R]} 귀속 손실 매출
    tot_waste_val = np.zeros(R); any_short = np.zeros(R, bool); n_short_pairs = np.zeros(R)
    for sid, g in pairs.groupby("store_id", sort=True):
        iids = list(g["ingredient_id"])
        I = len(iids)
        short_frac = np.zeros((R, I, H)); results = {}
        for k, iid in enumerate(iids):
            r = req.loc[(sid, iid)].to_numpy(dtype=float) if (sid, iid) in req.index else np.zeros(H)
            if scenarios is not None and sid in scn_ids[0] and iid in scn_ids[1]:
                rat = scenarios.ratios[:, scn_ids[0][sid], scn_ids[1][iid], :H]
                if rat.shape[1] < H:
                    rat = np.concatenate([rat, np.ones((N, H - rat.shape[1]))], axis=1)
            else:
                rat = np.ones((N, H))
            dem_all = np.vstack([r[None, :], r[None, :] * rat]) if N else r[None, :]
            li = lot_in[(lot_in["store_id"] == sid) & (lot_in["ingredient_id"] == iid)]
            pl = plan_n[(plan_n["store_id"] == sid) & (plan_n["ingredient_id"] == iid) & (plan_n["order_qty"] > 0)]
            q, arr, exps, new = [], [], [], []
            for x in li.itertuples(index=False):
                lu = pd.Timestamp(x.last_use_date) if pd.notna(x.last_use_date) else pd.Timestamp("2099-12-31")
                ad = pd.Timestamp(x.arrival_date) if pd.notna(x.arrival_date) else asof
                q.append(float(x.qty)); arr.append(0 if (x.source == "재고" or ad <= asof) else didx.get(ad, FAR))
                exps.append(int(np.searchsorted(dates.values, np.datetime64(lu), side="right")) if lu < dates[-1] else FAR)
                new.append(False)
            for x in pl.itertuples(index=False):
                lu = pd.Timestamp(x.last_use_date); ad = pd.Timestamp(x.arrival_date)
                q.append(float(x.use_qty)); arr.append(didx.get(ad, FAR))
                exps.append(int(np.searchsorted(dates.values, np.datetime64(lu), side="right")) if lu < dates[-1] else FAR)
                new.append(True)
            if q:
                q_a, arr_a, exp_a, new_a = np.array(q), np.array(arr), np.array(exps), np.array(new)
                order = np.lexsort((arr_a, exp_a))
                sim = _simulate_lots(q_a[order], arr_a[order], exp_a[order], new_a[order], dem_all)
            else:
                z = np.zeros_like(dem_all)
                sim = {"shortage": dem_all.copy(), "waste_exist": z, "waste_new": z.copy(), "on_hand_end": z.copy(), "consumed": z.copy()}
            w_end, w_txt = windows.get((sid, iid), default_win)
            short = sim["shortage"]
            with np.errstate(divide="ignore", invalid="ignore"):
                frac = np.where(dem_all > 1e-9, short / np.where(dem_all > 1e-9, dem_all, 1.0), 0.0)
            frac[:, w_end + 1:] = 0.0
            short_frac[:, k, :] = np.clip(frac, 0.0, 1.0)
            results[iid] = (r, dem_all, sim, w_end, w_txt, li, pl)
        # 메뉴 단위 손실 매출 (가장 모자란 재료 비율)
        lost = np.zeros(R); attr = {iid: np.zeros(R) for iid in iids}
        pos = {iid: k for k, iid in enumerate(iids)}
        for mid in [m for m in fc_menu.index.get_level_values("menu_id").unique() if (sid, m) in fc_menu.index]:
            b = bom[(bom["menu_id"] == mid) & (bom["ingredient_id"].isin(iids)) & (bom["qty_per_menu"].astype(float) > 0)]
            idx = [pos[i] for i in b["ingredient_id"]]
            if not idx:
                continue
            fcm = fc_menu.loc[(sid, mid)].to_numpy(dtype=float)
            sf = short_frac[:, idx, :]                       # [R, k, H]
            fm = sf.max(axis=1)                              # [R, H]
            am = sf.argmax(axis=1)                           # [R, H] → idx 내 위치
            lost_m = price_m.get(mid, 0.0) * fcm[None, :] * fm
            lost += lost_m.sum(axis=1)
            for j, kk in enumerate(idx):
                attr[iids[kk]] += np.where(am == j, lost_m, 0.0).sum(axis=1)
        store_lost[sid] = lost; store_attr[sid] = attr
        for iid in iids:
            r, dem_all, sim, w_end, w_txt, li, pl = results[iid]
            c = cost_pu.get(iid, 0.0)
            short = sim["shortage"]; waste = sim["waste_exist"] + sim["waste_new"]
            short_w = short[:, :w_end + 1].sum(axis=1); short_h = short.sum(axis=1)
            waste_tot = waste.sum(axis=1); waste_val = waste_tot * c
            lost_i = attr[iid]
            loss = lost_i + waste_val
            tot_waste_val += waste_val; any_short |= short_w > 1e-9; n_short_pairs += (short_w > 1e-9)
            mean_req = float(r.mean()) if H else 0.0
            end0 = float(sim["on_hand_end"][0, -1])
            first_so = next((dates[h] for h in range(H) if short[0, h] > 1e-9), pd.NaT)
            s_w, s_h, w_t, l_i, lo = short_w[1:], short_h[1:], waste_tot[1:], lost_i[1:], loss[1:]
            pct = (lambda a, p: float(np.percentile(a, p)) if len(a) else np.nan)
            line_rows.append({
                "store_id": sid, "ingredient_id": iid, "ingredient_name": names.get(iid, iid), "use_unit": units.get(iid),
                "window": w_txt, "window_end": dates[w_end], "window_days": int(w_end + 1),
                "plan_order_qty": int(pl["order_qty"].sum()) if len(pl) else 0,
                "plan_use_qty": float(pl["use_qty"].sum()) if len(pl) else 0.0,
                "plan_amount": float(pl["amount"].fillna(0).sum()) if len(pl) else 0.0,
                "plan_arrivals": ";".join(str(pd.Timestamp(a).date()) for a in pl["arrival_date"]) if len(pl) else "",
                "demand_window": float(r[:w_end + 1].sum()), "demand_horizon": float(r.sum()),
                "on_hand_asof": float(li.loc[li["source"] == "재고", "qty"].sum()) if len(li) else 0.0,
                "inbound_po_qty": float(li.loc[li["source"] == "입고예정", "qty"].sum()) if len(li) else 0.0,
                # 기대 경로
                "exp_shortage_window": float(short_w[0]), "exp_shortage_horizon": float(short_h[0]),
                "exp_stockout_days_window": int((short[0, :w_end + 1] > 1e-9).sum()), "exp_first_stockout": first_so,
                "exp_waste_exist_qty": float(sim["waste_exist"][0].sum()), "exp_waste_new_qty": float(sim["waste_new"][0].sum()),
                "exp_waste_qty": float(waste_tot[0]), "exp_waste_value": float(waste_val[0]),
                "exp_lost_revenue": float(lost_i[0]), "exp_loss": float(loss[0]),
                "exp_end_on_hand": end0, "exp_end_cover_days": float(end0 / mean_req) if mean_req > 0 else np.nan,
                # 시나리오 분포
                "n_scenarios": int(N), "p_stockout_window": float((s_w > 1e-9).mean()) if N else np.nan,
                "shortage_window_mean": float(s_w.mean()) if N else np.nan, "shortage_window_p90": pct(s_w, 90),
                "shortage_horizon_mean": float(s_h.mean()) if N else np.nan,
                "waste_mean": float(w_t.mean()) if N else np.nan, "waste_p90": pct(w_t, 90),
                "lost_revenue_mean": float(l_i.mean()) if N else np.nan, "lost_revenue_p90": pct(l_i, 90),
                "waste_value_mean": float((w_t * c).mean()) if N else np.nan,
                "loss_mean": float(lo.mean()) if N else np.nan, "loss_p90": pct(lo, 90),
                "cost_per_use_unit": c,
            })
            for h in range(H):
                daily_rows.append({"store_id": sid, "ingredient_id": iid, "date": dates[h], "in_window": int(h <= w_end),
                                   "demand": float(r[h]), "on_hand_end": float(sim["on_hand_end"][0, h]),
                                   "shortage": float(short[0, h]), "waste": float(waste[0, h]), "consumed": float(sim["consumed"][0, h]),
                                   "shortage_p90": pct(short[1:, h], 90) if N else float(short[0, h]),
                                   "on_hand_p10": pct(sim["on_hand_end"][1:, h], 10) if N else float(sim["on_hand_end"][0, h])})
    lines = pd.DataFrame(line_rows)
    dly = pd.DataFrame(daily_rows)
    tot_lost = np.sum([v for v in store_lost.values()], axis=0) if store_lost else np.zeros(R)
    tot_loss = tot_lost + tot_waste_val
    purchase = float(plan_n["amount"].fillna(0).sum()) if len(plan_n) else 0.0
    scn_rows = pd.DataFrame({"scenario": np.arange(1, N + 1),
                             "start_date": list(scenarios.start_dates)[:N] if scenarios is not None else [],
                             "lost_revenue": tot_lost[1:], "waste_value": tot_waste_val[1:], "total_loss": tot_loss[1:],
                             "any_stockout": any_short[1:]}) if N else pd.DataFrame(
        columns=["scenario", "start_date", "lost_revenue", "waste_value", "total_loss", "any_stockout"])
    totals = {
        "plan": name, "horizon_days": H, "window_mode": str(window), "n_scenarios": int(N), "purchase_amount": purchase,
        "n_order_lines": int((plan_n["order_qty"] > 0).sum()) if len(plan_n) else 0,
        "exp_lost_revenue": float(tot_lost[0]), "exp_waste_value": float(tot_waste_val[0]), "exp_loss": float(tot_loss[0]),
        "exp_stockout_pairs_window": int((lines["exp_shortage_window"] > 1e-9).sum()) if len(lines) else 0,
        "exp_stockout_pairs_horizon": int((lines["exp_shortage_horizon"] > 1e-9).sum()) if len(lines) else 0,
        "exp_waste_pairs": int((lines["exp_waste_qty"] > 1e-9).sum()) if len(lines) else 0,
        "mean_lost_revenue": float(tot_lost[1:].mean()) if N else float(tot_lost[0]),
        "mean_waste_value": float(tot_waste_val[1:].mean()) if N else float(tot_waste_val[0]),
        "mean_loss": float(tot_loss[1:].mean()) if N else float(tot_loss[0]),
        "p90_loss": float(np.percentile(tot_loss[1:], 90)) if N else float(tot_loss[0]),
        "p95_loss": float(np.percentile(tot_loss[1:], 95)) if N else float(tot_loss[0]),
        "p_any_stockout": float(any_short[1:].mean()) if N else float(any_short[0]),
        "mean_stockout_pairs": float(n_short_pairs[1:].mean()) if N else float(n_short_pairs[0]),
        "p90_stockout_pairs": float(np.percentile(n_short_pairs[1:], 90)) if N else float(n_short_pairs[0]),
    }
    totals["mean_loss_plus_purchase"] = totals["mean_loss"] + purchase
    by_store = pd.DataFrame([{"store_id": s, "purchase_amount": float(lines.loc[lines["store_id"] == s, "plan_amount"].sum()),
                              "exp_lost_revenue": float(store_lost[s][0]), "mean_lost_revenue": float(store_lost[s][1:].mean()) if N else float(store_lost[s][0]),
                              "exp_waste_value": float(lines.loc[lines["store_id"] == s, "exp_waste_value"].sum()),
                              "mean_waste_value": float(lines.loc[lines["store_id"] == s, "waste_value_mean"].sum()) if N else np.nan,
                              "stockout_pairs_window": int((lines.loc[lines["store_id"] == s, "exp_shortage_window"] > 1e-9).sum())}
                             for s in store_lost])
    assumptions += [
        "평가 창 = 내일부터 그 다음 발주분 도착 전날까지 (공급사 달력). 창 밖의 품절은 다음 발주 결정의 몫이라 이 발주안의 손실로 세지 않음"
        if window == "cycle" else f"평가 창 = {window}",
        "폐기는 기간 전체를 봄 (이후 추가 발주 없음 가정). FEFO 라 나중 로트가 먼저 들어온 로트의 소진을 막지 않아 신규 로트 폐기 추정에는 영향이 작음",
        "하루 순서: 07시 입고 → 마지막 사용일이 지난 로트 폐기 → 소요 FEFO 차감 → 기말 (명세 6절, 재고 전개와 동일)",
        "시나리오 = 과거에 같은 예측식을 적용한 식자재 단위 실제/예측 비율을 기간 길이 블록으로 잘라 미래 예측에 곱함 (분포 가정 없음, 매장·식자재 동시성 보존)",
        "품절 손실 매출 = Σ 메뉴가격 × 예측 판매 × (그 메뉴 재료 중 가장 모자란 재료의 부족 비율). 식자재별 귀속은 가장 모자란 재료에",
        "폐기 비용 = 폐기 수량 × 기준일 단가 ÷ 발주단위당 사용량. 발주 금액은 손실이 아니라 지출로 별도 표시",
        "부족분은 이월되지 않음 (그날 못 판 수요는 사라짐). 실사 조정·이동·기타 손실 0 가정",
    ]
    return PlanEvaluation(name, plan_n, lines, dly, totals, by_store, scn_rows, assumptions)


def compare_plans(evals: list[PlanEvaluation]) -> pd.DataFrame:
    """여러 발주안 평가 결과를 한 표로 (계획 (3) 비교용)."""
    rows = []
    for e in evals:
        t = e.totals
        rows.append({"발주안": e.name, "발주 라인": t["n_order_lines"], "발주 금액": round(t["purchase_amount"]),
                     "기대 품절 손실매출": round(t["mean_lost_revenue"]), "기대 폐기 비용": round(t["mean_waste_value"]),
                     "기대 총손실": round(t["mean_loss"]), "p90 총손실": round(t["p90_loss"]),
                     "기대 품절 품목 수(창 내)": round(t["mean_stockout_pairs"], 1),
                     "품절 확률(창 내 1건 이상)": round(t["p_any_stockout"], 2),
                     "기대 경로 품절 품목(창 내)": t["exp_stockout_pairs_window"], "기대 경로 손실": round(t["exp_loss"]),
                     "시나리오 수": t["n_scenarios"]})
    return pd.DataFrame(rows)
