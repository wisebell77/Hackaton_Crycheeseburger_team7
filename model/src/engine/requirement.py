"""
예측 수요 → 식자재 소요량 (design_notes 2.3) 과 안전재고용 σ, 그리고 발주안 평가가 쓰는 과거 잔차 배열.

R[s,i,t] = Σ_m F[s,m,t] × qty_per_menu[m,i]   (use_unit, 세트는 세트 자체 BOM 행 — prep.menu_qty_to_ingredients)

σ (두 가지)
- ingredient_sigma_backtest : 같은 예측식을 과거에 매일 적용했을 때의 식자재 단위 잔차 RMSE 를 커버일수(1~7일 합계)별로 잰다.
  메뉴 오차가 BOM 으로 합쳐질 때 서로 상쇄되는 효과가 반영된다 (실데이터: 메뉴 WAPE 28% → 식자재 1~3일 11%).
  급변(최근 2주/8주 소요 비율이 밴드 밖) 품목은 플래그를 세운다 (발주 단계에서 σ × shift_sigma_mult).
- ingredient_sigma          : 메뉴 σ × BOM 단순 합 (이전 방식, 보수적). 이력이 짧아 백테스트가 안 될 때의 대체.

backtest_ingredient_arrays : 위 백테스트의 중간 산출(식자재 단위 일별 예측·실제)을 돌려준다. evaluate.py 가 과거 잔차
  시나리오(실제/예측 비율 블록)를 만들 때 같은 배열을 쓴다.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

import numpy as np
import pandas as pd

from .. import prep
from ..loader import Dataset, require
from .params import EngineParams


def ingredient_requirement(forecast_df: pd.DataFrame, ds: Dataset) -> pd.DataFrame:
    """매장×식자재×일 예측 소요량.

    반환: date, store_id, ingredient_id, requirement_qty(use_unit), use_unit
    BOM 이 있는 매장×식자재 조합은 소요량이 0 인 날도 행을 남긴다 (재고 전개에서 날짜가 비지 않게).
    """
    bom = require(ds, "04_bom", ["menu_id", "ingredient_id", "qty_per_menu"])
    req = prep.menu_qty_to_ingredients(forecast_df, bom, "forecast_qty")
    dates = pd.DatetimeIndex(sorted(forecast_df["date"].unique()))
    pairs = req[["store_id", "ingredient_id"]].drop_duplicates()
    grid = pairs.merge(pd.DataFrame({"date": dates}), how="cross")
    out = grid.merge(req, on=["date", "store_id", "ingredient_id"], how="left", validate="one_to_one")
    out["requirement_qty"] = out["requirement_qty"].fillna(0.0)
    ing = ds.get("03_ingredients")
    if ing is not None and "use_unit" in ing.columns:
        out = out.merge(ing[["ingredient_id", "use_unit"]].drop_duplicates("ingredient_id"), on="ingredient_id",
                        how="left", validate="many_to_one")
    else:
        out["use_unit"] = None
    return out[["date", "store_id", "ingredient_id", "requirement_qty", "use_unit"]].sort_values(
        ["store_id", "ingredient_id", "date"]).reset_index(drop=True)


def ingredient_sigma(basis_df: pd.DataFrame, ds: Dataset) -> pd.DataFrame:
    """메뉴 σ(일 단위 수요 표준편차)를 BOM 으로 식자재 σ 로 합친다 (단순 합, 보수적).

    반환: store_id, ingredient_id, sigma_daily(use_unit/일), method="menu_sum"
    """
    bom = require(ds, "04_bom", ["menu_id", "ingredient_id", "qty_per_menu"])
    m = basis_df[["store_id", "menu_id", "sigma"]].merge(bom[["menu_id", "ingredient_id", "qty_per_menu"]],
                                                         on="menu_id", how="inner", validate="many_to_many")
    m["sigma_part"] = m["sigma"].astype(float) * m["qty_per_menu"].astype(float)
    out = m.groupby(["store_id", "ingredient_id"], as_index=False)["sigma_part"].sum().rename(
        columns={"sigma_part": "sigma_daily"})
    out["method"] = "menu_sum"
    return out


@dataclass
class BacktestArrays:
    """같은 예측식을 과거에 매일 적용한 결과 (식자재 단위). 날짜축 = 이력 시작 ~ 기준일.

    fc_ing / act_ing : [S, I, T] 그날 예측 소요 / 실제 수요×BOM. 같은 요일 표본이 2회 이상인 메뉴-일만 합산 (둘 다 같은 마스크).
    act_all          : [S, I, T] 취급 메뉴 전체의 실제 수요×BOM (급변 비율용).
    eval_start       : 평가 시작 인덱스 (이력 시작 + eval_skip_days).
    """
    stores: list[str]
    ingredients: list[str]
    dates: pd.DatetimeIndex
    fc_ing: np.ndarray
    act_ing: np.ndarray
    act_all: np.ndarray
    eval_start: int


def backtest_ingredient_arrays(ds: Dataset, daily: pd.DataFrame, params: EngineParams, min_days: int = 35,
                               eval_skip_days: int = 28) -> Optional[BacktestArrays]:
    """예측식(같은 요일 최근 N회 평균 × 공휴일 계수 × 취급, 추세 없음)을 기준일 이전 모든 날에 적용해 식자재 단위로 합산한다.

    목표일 t 의 예측은 t−7k (k=1..max_lookback_weeks) 중 공휴일·미취급·첫 판매 전이 아닌 최근 N개 평균 × 공휴일 계수.
    t−7 ≤ 기준일이면 되므로 '기준일 i 에서 1~7일 앞' 예측이 날짜별로 하나로 정해진다 (vectorized).
    이력이 min_days 미만이면 None.
    """
    from .forecast import _demand_col, holiday_factor_for, holiday_factors  # 순환 import 방지

    col = _demand_col(daily, None)
    asof = params.asof
    hist = daily[daily["date"] <= asof]
    if hist.empty:
        return None
    dates = pd.date_range(hist["date"].min(), asof)
    T = len(dates)
    if T < min_days:
        return None
    stores = sorted(hist["store_id"].unique()); menus = sorted(hist["menu_id"].unique())
    si = {s: k for k, s in enumerate(stores)}; mi = {m: k for k, m in enumerate(menus)}
    ti = {d: k for k, d in enumerate(dates)}
    S, M = len(stores), len(menus)
    X = np.zeros((S, M, T)); A = np.zeros((S, M, T), dtype=bool)
    s_idx = hist["store_id"].map(si).to_numpy(); m_idx = hist["menu_id"].map(mi).to_numpy()
    t_idx = hist["date"].map(ti).to_numpy()
    X[s_idx, m_idx, t_idx] = hist[col].to_numpy(dtype=float)
    A[s_idx, m_idx, t_idx] = (hist["is_available"] == 1).to_numpy()
    pos = (X > 0) & A                                   # 첫 판매 전 = 취급 중 주문 없음 → 제외
    first = np.where(pos.any(axis=2), pos.argmax(axis=2), T)
    A &= np.arange(T)[None, None, :] >= first[:, :, None]
    cal = prep.calendar_features(ds, dates)
    hol_t = cal["is_holiday"].to_numpy().astype(bool); wd_t = cal["weekday_iso"].to_numpy().astype(int)
    hol = holiday_factors(hist, col)
    hf = np.array([[holiday_factor_for(hol, s, bool(hol_t[t]), int(wd_t[t]), params.apply_holiday) for t in range(T)]
                   for s in stores])
    n_keep = max(int(params.lookback_weeks), 1)
    K = max(int(params.max_lookback_weeks), n_keep)
    bsum = np.zeros((S, M, T)); bcnt = np.zeros((S, M, T))
    for k in range(1, K + 1):
        sh = 7 * k
        if sh >= T:
            break
        val = X[:, :, :T - sh]
        elig = A[:, :, :T - sh] & ~hol_t[None, None, :T - sh]
        take = elig & (bcnt[:, :, sh:] < n_keep)
        bsum[:, :, sh:] += np.where(take, val, 0.0)
        bcnt[:, :, sh:] += take
    base = np.where(bcnt > 0, bsum / np.maximum(bcnt, 1), 0.0)
    fc = base * hf[:, None, :] * A
    valid = (bcnt >= 2) & A
    bom = require(ds, "04_bom", ["menu_id", "ingredient_id", "qty_per_menu"])
    ings = sorted(bom["ingredient_id"].unique()); ii = {i: k for k, i in enumerate(ings)}
    B = np.zeros((M, len(ings)))
    for r in bom.itertuples(index=False):
        if r.menu_id in mi:
            B[mi[r.menu_id], ii[r.ingredient_id]] += float(r.qty_per_menu)
    fc_ing = np.einsum("smt,mi->sit", np.where(valid, fc, 0.0), B)
    act_ing = np.einsum("smt,mi->sit", np.where(valid, X, 0.0), B)
    act_all = np.einsum("smt,mi->sit", X * A, B)
    t0 = min(eval_skip_days, max(T - 8, 0))
    return BacktestArrays(stores, ings, dates, fc_ing, act_ing, act_all, t0)


def ingredient_sigma_backtest(ds: Dataset, daily: pd.DataFrame, params: EngineParams,
                              notes: Optional[list[str]] = None, max_L: int = 7, min_days: int = 35,
                              eval_skip_days: int = 28, bt: Optional[BacktestArrays] = None) -> Optional[pd.DataFrame]:
    """식자재 단위 커버일수별 σ (백테스트 잔차의 L일 연속 합 RMSE) + 급변 플래그.

    반환: store_id, ingredient_id, sigma_daily(=σ_1), sigma_by_L(list), mean_by_L(list), cv_3d, n_obs, eval_days,
          shift_ratio, shift_flag, method="backtest"   (σ 는 demand_multiplier 배율 반영). 이력이 짧으면 None.
    """
    if bt is None:
        bt = backtest_ingredient_arrays(ds, daily, params, min_days=min_days, eval_skip_days=eval_skip_days)
    if bt is None:
        if notes is not None:
            notes.append(f"이력이 {min_days}일 미만이라 식자재 단위 백테스트 σ 를 계산하지 못함 → 메뉴 σ 합산 사용")
        return None
    S, I, T = bt.fc_ing.shape
    t0 = bt.eval_start
    Ri = bt.fc_ing - bt.act_ing
    csR = np.concatenate([np.zeros((S, I, 1)), Ri.cumsum(axis=2)], axis=2)
    csY = np.concatenate([np.zeros((S, I, 1)), bt.act_ing.cumsum(axis=2)], axis=2)
    sig = np.full((S, I, max_L), np.nan); meanY = np.full((S, I, max_L), np.nan); nobs = np.zeros(max_L, int)
    for L in range(1, max_L + 1):
        starts = np.arange(t0, T - L + 1)
        if len(starts) < 5:
            break
        RL = csR[:, :, starts + L] - csR[:, :, starts]
        YL = csY[:, :, starts + L] - csY[:, :, starts]
        sig[:, :, L - 1] = np.sqrt((RL ** 2).mean(axis=2)); meanY[:, :, L - 1] = YL.mean(axis=2); nobs[L - 1] = len(starts)
    n_L = int((nobs > 0).sum())
    if n_L == 0:
        return None
    r2 = bt.act_all[:, :, max(0, T - 14):].mean(axis=2); r8 = bt.act_all[:, :, max(0, T - 56):].mean(axis=2)
    with np.errstate(divide="ignore", invalid="ignore"):
        ratio = np.where(r8 > 0, r2 / np.where(r8 > 0, r8, 1.0), np.nan)
    lo, hi = params.shift_band
    flag = np.where(np.isnan(ratio), False, (ratio >= hi) | (ratio <= lo))
    scen = float(params.demand_multiplier)
    rows: list[dict[str, Any]] = []
    for a, s in enumerate(bt.stores):
        for b, i in enumerate(bt.ingredients):
            by_L = [float(v) * scen for v in sig[a, b, :n_L]]
            m3 = meanY[a, b, min(2, n_L - 1)]
            rows.append({"store_id": s, "ingredient_id": i, "sigma_daily": by_L[0], "sigma_by_L": by_L,
                         "mean_by_L": [float(v) for v in meanY[a, b, :n_L]],
                         "cv_3d": float(sig[a, b, min(2, n_L - 1)] / m3) if m3 and m3 > 0 else np.nan,
                         "n_obs": int(nobs[0]), "eval_days": int(T - t0),
                         "shift_ratio": None if np.isnan(ratio[a, b]) else float(ratio[a, b]),
                         "shift_flag": bool(flag[a, b]), "method": "backtest"})
    if notes is not None:
        txt = (f"식자재 σ = 같은 예측식을 {bt.dates[t0].date()}~{bt.dates[-1].date()} 매일 적용한 잔차의 "
               f"커버일수별(1~{n_L}일 합계) RMSE (메뉴 σ 합산이 아니라 식자재 단위로 직접 측정)")
        if txt not in notes:
            notes.append(txt)
        n_flag = int(flag.sum())
        if n_flag:
            notes.append(f"급변 품목 {n_flag}개 (최근 2주/8주 소요 비율이 {lo}~{hi} 밖) → σ × {params.shift_sigma_mult:.1f}")
    return pd.DataFrame(rows)


def menu_contributions(forecast_df: pd.DataFrame, ds: Dataset, store_id: str, ingredient_id: str,
                       start: Optional[pd.Timestamp] = None, end: Optional[pd.Timestamp] = None) -> pd.DataFrame:
    """한 매장·식자재의 메뉴별 소요 기여 (설명 패널용). start ≤ date < end 구간.

    반환: menu_id, menu_name, qty_per_menu, forecast_qty(구간 합, 메뉴 개수), requirement_qty(use_unit), share
    """
    bom = require(ds, "04_bom", ["menu_id", "ingredient_id", "qty_per_menu"])
    b = bom[bom["ingredient_id"] == ingredient_id][["menu_id", "qty_per_menu"]]
    f = forecast_df[forecast_df["store_id"] == store_id]
    if start is not None:
        f = f[f["date"] >= start]
    if end is not None:
        f = f[f["date"] < end]
    g = f.groupby("menu_id", as_index=False)["forecast_qty"].sum()
    out = b.merge(g, on="menu_id", how="inner", validate="many_to_one")
    out["requirement_qty"] = out["forecast_qty"] * out["qty_per_menu"].astype(float)
    tot = out["requirement_qty"].sum()
    out["share"] = out["requirement_qty"] / tot if tot > 0 else 0.0
    menus = ds.get("02_menus")
    if menus is not None and "menu_name" in menus.columns:
        out = out.merge(menus[["menu_id", "menu_name"]].drop_duplicates("menu_id"), on="menu_id", how="left",
                        validate="many_to_one")
    else:
        out["menu_name"] = out["menu_id"]
    cols = ["menu_id", "menu_name", "qty_per_menu", "forecast_qty", "requirement_qty", "share"]
    return out[cols].sort_values("requirement_qty", ascending=False).reset_index(drop=True)
