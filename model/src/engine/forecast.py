"""
메뉴 단위 수요 예측 (규칙 기반, 설명 가능) — design_notes 2.2 + 실데이터 백테스트(2026-10-31) 반영.

F[s,m,t] = base(같은 요일 최근 4회 평균) × 공휴일 계수(매장별, 평일/주말 공휴일 구분) × 프로모션 계수 × 취급 여부 × 시나리오 배율
           (× 추세 계수 — 기본 꺼짐. 백테스트에서 최근 2주/8주 추세 보정이 메뉴·식자재 오차를 모두 키웠다)

- 입력은 demand_qty (품절 전 수요).
- base: 목표일에서 7일씩 거슬러 올라가며 공휴일·미취급일·'첫 판매 전' 날을 건너뛰고 최근 lookback_weeks 개(기본 4)를 모은다
  (최대 max_lookback_weeks 주, 기본 6). 예: 10/1(목)은 9/24 가 추석 연휴면 9/17, 9/10, 9/3, 8/27 을 쓴다.
- '취급 중 주문 없음'(취급표상 취급이지만 첫 판매 전) 구간은 base 에서 뺀다 (명세서: 미취급·품절 손실·주문 없음 구분).
- 공휴일 계수 = 공휴일 실제 매장 수요 ÷ 같은 요일 ±4주 비공휴일 평균. 평일 공휴일과 주말 공휴일을 따로 둔다 (표본 3일 미만이면 전체 계수).
- 기준일 D 이전(≤ D) 데이터만 쓴다. 미래 정보는 달력·프로모션 계획·취급 기간표뿐.
- basis_df 에 매장×메뉴별로 base 에 쓴 실제 값, 각 계수와 근거 문장을 남긴다 (설명 패널용).

백테스트 성적 (실데이터, 2026-03-01~09-30, 매일 기준일, 1~7일 앞): 메뉴 WAPE 27.7%, 식자재 1~3일 합계 WAPE 10.8%
(7일 이동평균 13.3%, 같은 식 + 추세 12.4%).
"""
from __future__ import annotations

from typing import Any, Optional

import numpy as np
import pandas as pd

from .. import prep
from ..loader import Dataset
from .params import EngineParams

WEEKDAY_KO = "월화수목금토일"
TREND_CLIP = (0.7, 1.3)
HOLIDAY_CLIP = (0.5, 2.0)
PROMO_CLIP = (0.8, 3.0)
# 07_calendar 가 예측 기간을 덮지 않을 때 쓰는 10월 공휴일 (가정, 확인 필요)
FALLBACK_HOLIDAYS = {
    pd.Timestamp("2026-10-03"): "개천절",
    pd.Timestamp("2026-10-05"): "대체공휴일(개천절)",
    pd.Timestamp("2026-10-09"): "한글날",
}


def _note(notes: Optional[list[str]], text: str) -> None:
    """가정 목록에 중복 없이 한 줄 추가."""
    if notes is not None and text not in notes:
        notes.append(text)


def _demand_col(daily: pd.DataFrame, notes: Optional[list[str]]) -> str:
    """예측 입력 컬럼. demand_qty 가 없으면 qty 로 대체(가정)."""
    if "demand_qty" in daily.columns:
        return "demand_qty"
    _note(notes, "09_sales 에 demand_qty 가 없어 qty 로 수요를 대신함 (품절일 과소예측 가능)")
    return "qty"


def future_calendar(ds: Dataset, dates: pd.DatetimeIndex, notes: Optional[list[str]] = None) -> pd.DataFrame:
    """예측 날짜의 요일·공휴일 (date, weekday_iso, is_holiday, holiday_name).

    07_calendar 가 날짜를 덮으면 그 값을, 아니면 날짜로 요일을 계산하고 FALLBACK_HOLIDAYS 를 공휴일로 본다.
    """
    cal = ds.get("07_calendar")
    base = prep.calendar_features(ds, pd.DatetimeIndex(dates))
    if "holiday_name" not in base.columns:
        base["holiday_name"] = None
    covered = set(cal["date"].dropna()) if cal is not None and "date" in cal.columns else set()
    missing = [d for d in dates if d not in covered]
    if missing:
        for d in missing:
            idx = base.index[base["date"] == d]
            if d in FALLBACK_HOLIDAYS:
                base.loc[idx, "is_holiday"] = 1
                base.loc[idx, "holiday_name"] = FALLBACK_HOLIDAYS[d]
        _note(notes, f"07_calendar 가 예측 기간 {len(missing)}일을 덮지 않아 요일은 날짜로 계산하고 "
                     "2026-10-03·10-05·10-09 를 공휴일로 가정")
    return base[["date", "weekday_iso", "is_holiday", "holiday_name"]]


def future_availability(ds: Dataset, dates: pd.DatetimeIndex, asof: pd.Timestamp,
                        notes: Optional[list[str]] = None) -> pd.DataFrame:
    """예측 날짜의 매장×메뉴 취급 여부 (store_id, menu_id, date, is_available, availability_source).

    취급 기간표가 그 날짜를 덮지 않으면(기간이 기준일에 끝남 등) 기준일의 상태가 이어진다고 본다 (가정).
    """
    grid = prep.availability_grid(ds, pd.DatetimeIndex([asof]).append(pd.DatetimeIndex(dates)))
    if grid.empty:
        return grid
    at_asof = grid[grid["date"] == asof][["store_id", "menu_id", "is_available"]].rename(
        columns={"is_available": "_asof_avail"})
    fut = grid[grid["date"] > asof].merge(at_asof, on=["store_id", "menu_id"], how="left", validate="many_to_one")
    gap = fut["availability_source"] == "기간표에 날짜 없음"
    if gap.any():
        fut.loc[gap, "is_available"] = fut.loc[gap, "_asof_avail"].fillna(0).astype(int)
        fut.loc[gap, "availability_source"] = "기준일 상태 유지(가정)"
        _note(notes, "메뉴 취급 기간표가 예측 기간을 덮지 않는 매장×메뉴는 기준일의 취급/미취급 상태가 이어진다고 가정")
    return fut.drop(columns="_asof_avail").reset_index(drop=True)


def _weekday_expected(hist: pd.DataFrame, col: str, mask: pd.Series) -> pd.Series:
    """mask 행(평상일)의 매장×메뉴×요일 평균 → hist 각 행의 '같은 요일 평상일 평균'."""
    ref = (hist[mask].groupby(["store_id", "menu_id", "weekday_iso"])[col].mean().rename("_exp"))
    return hist.join(ref, on=["store_id", "menu_id", "weekday_iso"])["_exp"]


def holiday_factors(hist: pd.DataFrame, col: str) -> dict[str, dict[str, Any]]:
    """매장별 공휴일 계수 (메뉴 합계, 매장 단위).

    공휴일 하루의 기대값 = 같은 요일 ±4주(최대 8일) 비공휴일 매장 수요 평균. 계수 = Σ 실제 / Σ 기대값.
    평일 공휴일(월~금)과 주말 공휴일(토·일)을 따로 구한다 (표본 3일 미만이면 전체 공휴일 계수로 대체).
    공휴일 표본이 전혀 없으면 주말 계수(주말 평균/평일 평균, 평일 공휴일에만 적용), 그것도 없으면 1.0.
    반환: {store_id: {'factor'(전체), 'factor_weekday', 'factor_weekend', 'weekend_factor', 'n_holidays',
                      'n_weekday', 'n_weekend', 'note', 'source', 'detail'}}
    """
    out: dict[str, dict[str, Any]] = {}
    ok = hist["is_available"] == 1
    tot = (hist[ok].groupby(["store_id", "date"], as_index=False)
           .agg(q=(col, "sum"), is_holiday=("is_holiday", "max"), weekday_iso=("weekday_iso", "first")))
    for sid, part in tot.groupby("store_id"):
        s = part.set_index("date")["q"]
        hol_dates = part.loc[part["is_holiday"] == 1, ["date", "weekday_iso"]]
        hol_set = set(hol_dates["date"])
        detail = []
        for d, w in hol_dates.itertuples(index=False):
            cands = [d + pd.Timedelta(days=7 * k) for k in range(-4, 5) if k != 0]
            vals = [float(s[c]) for c in cands if c in s.index and c not in hol_set]
            if vals:
                detail.append({"date": d, "weekday_iso": int(w), "actual": float(s[d]), "expected": float(np.mean(vals)),
                               "weekend": int(w) >= 6})
        norm = part[part["is_holiday"] == 0]
        we, wd = norm[norm["weekday_iso"] >= 6]["q"].mean(), norm[norm["weekday_iso"] <= 5]["q"].mean()
        weekend = float(we / wd) if pd.notna(we) and pd.notna(wd) and wd > 0 else 1.0
        if detail:
            df = pd.DataFrame(detail)

            def ratio(sub: pd.DataFrame) -> float:
                a, e = float(sub["actual"].sum()), float(sub["expected"].sum())
                return a / e if e > 0 else float("nan")

            raw_all = ratio(df)
            f_all = float(np.clip(raw_all, *HOLIDAY_CLIP))
            parts: dict[str, tuple[float, int]] = {}
            for key, flag in (("weekday", False), ("weekend", True)):
                sub = df[df["weekend"] == flag]
                r = ratio(sub) if len(sub) >= 3 else float("nan")
                parts[key] = (float(np.clip(r, *HOLIDAY_CLIP)) if pd.notna(r) else f_all, int(len(sub)))
            note = (f"과거 공휴일 {len(df)}일 (실제 ÷ 같은 요일 ±4주 비공휴일 평균): 전체 {raw_all:.2f}, "
                    f"평일 공휴일 {parts['weekday'][1]}일 → {parts['weekday'][0]:.2f}, "
                    f"주말 공휴일 {parts['weekend'][1]}일 → {parts['weekend'][0]:.2f} "
                    f"(표본 3일 미만이면 전체 계수, 클립 {HOLIDAY_CLIP[0]}~{HOLIDAY_CLIP[1]})")
            out[sid] = {"factor": f_all, "factor_weekday": parts["weekday"][0], "factor_weekend": parts["weekend"][0],
                        "weekend_factor": weekend, "n_holidays": int(len(df)), "n_weekday": parts["weekday"][1],
                        "n_weekend": parts["weekend"][1], "note": note, "source": "holiday", "detail": detail}
        else:
            out[sid] = {"factor": None, "factor_weekday": None, "factor_weekend": None, "weekend_factor": weekend,
                        "n_holidays": 0, "n_weekday": 0, "n_weekend": 0, "source": "weekend", "detail": [],
                        "note": f"과거 공휴일 표본 없음 → 평일 공휴일에는 주말 계수 {weekend:.2f} 사용"}
    return out


def holiday_factor_for(hol: dict[str, dict[str, Any]], store_id: str, is_holiday: int | bool, weekday_iso: int,
                       apply: bool = True) -> float:
    """어떤 날의 공휴일 계수. 평일 공휴일/주말 공휴일 계수를 고르고, 표본이 없으면 주말 계수 대용."""
    if not apply or not is_holiday:
        return 1.0
    h = hol.get(store_id)
    if h is None:
        return 1.0
    if h.get("factor") is None:
        return float(h.get("weekend_factor", 1.0)) if int(weekday_iso) <= 5 else 1.0
    f = h.get("factor_weekend") if int(weekday_iso) >= 6 else h.get("factor_weekday")
    return float(f if f is not None else h["factor"])


def promo_factors(hist: pd.DataFrame, col: str) -> pd.DataFrame:
    """매장×메뉴 프로모션 계수 = Σ 행사일 실제 / Σ 같은 요일 비행사일 평균 (대상 메뉴 행사일만).

    매장×메뉴 표본이 없으면 같은 메뉴의 다른 매장 합계, 그다음 전체 대상 메뉴 합계, 마지막으로 1.0.
    반환: store_id, menu_id, promo_factor, promo_note
    """
    ok = (hist["is_available"] == 1) & (hist["is_holiday"] == 0)
    if "promo_target" not in hist.columns:
        hist = hist.assign(promo_target=0)
    normal = ok & (hist["promo_target"] == 0)
    h = hist.assign(_exp=_weekday_expected(hist, col, normal))
    promo = h[ok & (h["promo_target"] == 1)].dropna(subset=["_exp"])

    def ratio(df: pd.DataFrame) -> Optional[tuple[float, float, int]]:
        a, e = float(df[col].sum()), float(df["_exp"].sum())
        return (a, e, int(len(df))) if len(df) and e > 0 else None

    pooled = ratio(promo)
    by_menu = {m: ratio(g) for m, g in promo.groupby("menu_id")}
    by_pair = {k: ratio(g) for k, g in promo.groupby(["store_id", "menu_id"])}
    rows = []
    for (sid, mid) in hist[["store_id", "menu_id"]].drop_duplicates().itertuples(index=False):
        for level, r in (("이 매장·메뉴", by_pair.get((sid, mid))), ("같은 메뉴 전 매장", by_menu.get(mid)),
                         ("전체 행사 대상 메뉴", pooled)):
            if r is not None:
                a, e, n = r
                f = float(np.clip(a / e, *PROMO_CLIP))
                rows.append((sid, mid, f, f"{level} 행사일 {n}건 실제 {a:,.0f} / 같은 요일 평상일 평균 합 {e:,.0f} "
                                          f"= {a / e:.2f} (클립 → {f:.2f})"))
                break
        else:
            rows.append((sid, mid, 1.0, "과거 프로모션 표본 없음 → 1.0"))
    return pd.DataFrame(rows, columns=["store_id", "menu_id", "promo_factor", "promo_note"])


def _trend(part: pd.DataFrame, col: str, asof: pd.Timestamp, first_date: pd.Timestamp) -> tuple[float, str, float, float]:
    """추세 계수 = 최근 2주 평균 / 최근 8주 평균 (평상일, 0.7~1.3 클립). 이력이 8주 미만이면 1.0."""
    if first_date > asof - pd.Timedelta(days=55):
        return 1.0, "이력 8주 미만 → 추세 1.0", np.nan, np.nan
    r2 = part[part["date"] > asof - pd.Timedelta(days=14)][col]
    r8 = part[part["date"] > asof - pd.Timedelta(days=56)][col]
    if len(r2) < 7 or len(r8) < 28 or r8.mean() <= 0:
        return 1.0, "추세 표본 부족 → 1.0", float(r2.mean()) if len(r2) else np.nan, float(r8.mean()) if len(r8) else np.nan
    raw = float(r2.mean() / r8.mean())
    f = float(np.clip(raw, *TREND_CLIP))
    return f, f"최근 2주 평균 {r2.mean():.2f} / 최근 8주 평균 {r8.mean():.2f} = {raw:.2f} (클립 → {f:.2f})", \
        float(r2.mean()), float(r8.mean())


def _pooled_sigma(used: pd.DataFrame, col: str) -> float:
    """같은 요일 값들의 표준편차를 요일별로 구해 분산 평균의 제곱근 (표본 2개 이상인 요일만)."""
    var = used.groupby("weekday_iso")[col].agg(lambda x: x.var(ddof=1) if len(x) >= 2 else np.nan).dropna()
    if len(var):
        return float(np.sqrt(var.mean()))
    return float(used[col].std(ddof=1)) if len(used) >= 2 else 0.0


def first_sale_dates(hist: pd.DataFrame, col: str) -> dict[tuple[str, str], pd.Timestamp]:
    """매장×메뉴 첫 판매일 (취급일 중 수요 > 0 인 첫 날). 그 전은 '취급 중 주문 없음' 으로 보고 base 에서 뺀다."""
    pos = hist[(hist["is_available"] == 1) & (hist[col] > 0)]
    return pos.groupby(["store_id", "menu_id"])["date"].min().to_dict()


def _base_table(hist: pd.DataFrame, col: str, params: EngineParams) -> tuple[pd.DataFrame, pd.DataFrame]:
    """매장×메뉴 기초 통계 (basis 행)와 매장×메뉴×요일 base 표.

    요일별로 기준일에서 가까운 순서로 공휴일·미취급·첫 판매 전 날을 건너뛰며 lookback_weeks 개를 모은다
    (최대 max_lookback_weeks 주 안에서).
    """
    asof = params.asof
    n_keep = max(int(params.lookback_weeks), 1)
    max_weeks = max(int(params.max_lookback_weeks), n_keep)
    search_start = asof - pd.Timedelta(days=7 * max_weeks - 1)
    recent_start = asof - pd.Timedelta(days=7 * n_keep - 1)
    first_sale = first_sale_dates(hist, col)
    first_date = hist["date"].min()
    win = hist[(hist["date"] >= search_start) & (hist["date"] <= asof)]
    rows, wd_rows = [], []
    for (sid, mid), part in win.groupby(["store_id", "menu_id"]):
        fs = first_sale.get((sid, mid))
        part = part.sort_values("date", ascending=False)
        pre_first = pd.Series(True, index=part.index) if fs is None else (part["date"] < fs)
        reason = np.where(part["is_available"] != 1, "미취급",
                          np.where(part["is_holiday"] == 1, "공휴일", np.where(pre_first, "첫 판매 전", "")))
        part = part.assign(_reason=reason)
        elig = part[part["_reason"] == ""]
        used = elig.groupby("weekday_iso", sort=False).head(n_keep).sort_values("date")
        excluded = part[(part["_reason"] != "") & (part["date"] >= recent_start)].sort_values("date")
        by_wd = used.groupby("weekday_iso")[col].mean().to_dict()
        full = hist[(hist["store_id"] == sid) & (hist["menu_id"] == mid) & (hist["is_available"] == 1)
                    & (hist["is_holiday"] == 0)]
        t_raw, t_note, m2, m8 = _trend(full, col, asof, first_date)
        if params.apply_trend:
            t = t_raw
        else:
            t = 1.0
            t_note = f"{t_note} → 미적용 (실데이터 백테스트에서 추세 보정이 오차를 키워 apply_trend=False)"
        rows.append({
            "store_id": sid, "menu_id": mid, "lookback_start": recent_start, "search_start": search_start,
            "lookback_end": asof, "first_sale_date": fs,
            "base_by_weekday": {int(k): float(v) for k, v in by_wd.items()},
            "base_all_mean": float(used[col].mean()) if len(used) else np.nan,
            "base_values": [{"date": d, "weekday_iso": int(w), "demand_qty": float(q)}
                            for d, w, q in used[["date", "weekday_iso", col]].itertuples(index=False)],
            "excluded_values": [{"date": d, "weekday_iso": int(w), "demand_qty": float(q), "reason": r}
                                for d, w, q, r in excluded[["date", "weekday_iso", col, "_reason"]]
                                .itertuples(index=False)],
            "n_obs": int(len(used)), "sigma": _pooled_sigma(used, col),
            "trend_factor": t, "trend_raw": t_raw, "trend_note": t_note, "trend_recent2w_mean": m2, "trend_8w_mean": m8,
        })
        for w, v in by_wd.items():
            wd_rows.append((sid, mid, int(w), float(v)))
    basis = pd.DataFrame(rows)
    wd = pd.DataFrame(wd_rows, columns=["store_id", "menu_id", "weekday_iso", "base_wd"])
    return basis, wd


def _store_sizes(ds: Dataset) -> dict[str, float]:
    st = ds.get("01_stores")
    if st is None or "relative_size" not in st.columns:
        return {}
    return {r.store_id: float(r.relative_size) for r in st.itertuples(index=False) if pd.notna(r.relative_size)}


def forecast_menu_daily(ds: Dataset, params: EngineParams, daily: Optional[pd.DataFrame] = None,
                        notes: Optional[list[str]] = None) -> tuple[pd.DataFrame, pd.DataFrame]:
    """매장×메뉴×미래일 예측 수요.

    params 는 resolve_params 를 거친 값(asof_date 채워짐)이어야 한다.
    반환
      forecast_df: date, store_id, menu_id, weekday_iso, is_holiday, holiday_name, base_qty, base_source,
                   trend_factor, holiday_factor, promo_factor, is_available, scenario_factor, forecast_qty
      basis_df   : 매장×메뉴 1행. base_by_weekday, base_values(쓴 값 목록), excluded_values, trend/holiday/promo 계수와
                   근거 문장, sigma(같은 요일 표준편차, 시나리오 배율 반영), n_obs, menu_name, first_sale_date
    """
    asof = params.asof
    daily = prep.daily_sales(ds) if daily is None else daily
    col = _demand_col(daily, notes)
    hist = daily[daily["date"] <= asof].copy()
    dates = params.horizon_dates
    cal = future_calendar(ds, dates, notes)
    avail = future_availability(ds, dates, asof, notes)
    basis, wd = _base_table(hist, col, params)
    hol = holiday_factors(hist, col)
    promo = promo_factors(hist, col)
    sizes = _store_sizes(ds)

    pairs = avail[["store_id", "menu_id"]].drop_duplicates() if len(avail) else basis[["store_id", "menu_id"]]
    grid = pairs.merge(cal, how="cross").merge(
        avail[["store_id", "menu_id", "date", "is_available"]] if len(avail) else
        pairs.merge(cal[["date"]], how="cross").assign(is_available=1),
        on=["store_id", "menu_id", "date"], how="left", validate="one_to_one")
    grid["is_available"] = grid["is_available"].fillna(1).astype(int)
    grid = grid.merge(wd, on=["store_id", "menu_id", "weekday_iso"], how="left", validate="many_to_one")
    all_mean = basis.set_index(["store_id", "menu_id"])["base_all_mean"] if len(basis) else pd.Series(dtype=float)
    grid["base_source"] = np.where(grid["base_wd"].notna(), f"같은 요일 최근 {params.lookback_weeks}회 평균", None)
    grid["base_qty"] = grid["base_wd"]
    # 요일 표본이 없으면 모은 값 전체 평균
    miss = grid["base_qty"].isna()
    if miss.any():
        fill = [all_mean.get((s, m), np.nan) for s, m in zip(grid.loc[miss, "store_id"], grid.loc[miss, "menu_id"])]
        grid.loc[miss, "base_qty"] = fill
        grid.loc[miss & grid["base_qty"].notna(), "base_source"] = "요일 표본 없음 → 최근 전체 평균"
    # 이력이 아예 없으면 다른 매장 같은 요일 평균 × 매장 규모비 (콜드스타트, 가정)
    miss = grid["base_qty"].isna()
    if miss.any():
        for i in grid.index[miss]:
            s, m, w = grid.at[i, "store_id"], grid.at[i, "menu_id"], grid.at[i, "weekday_iso"]
            others = wd[(wd["menu_id"] == m) & (wd["weekday_iso"] == w) & (wd["store_id"] != s)]
            vals = [v * sizes.get(s, 1.0) / sizes.get(o, 1.0) for o, v in zip(others["store_id"], others["base_wd"])]
            if vals:
                grid.at[i, "base_qty"] = float(np.mean(vals))
                grid.at[i, "base_source"] = "이력 없음 → 다른 매장 평균 × 매장 규모비(가정)"
                _note(notes, "최근 이력이 없는 매장×메뉴는 다른 매장 같은 요일 평균 × relative_size 비율로 대체")
        grid["base_qty"] = grid["base_qty"].fillna(0.0)
        grid["base_source"] = grid["base_source"].fillna("이력 없음 → 0")

    trend = basis.set_index(["store_id", "menu_id"])["trend_factor"] if len(basis) else pd.Series(dtype=float)
    grid["trend_factor"] = [float(trend.get((s, m), 1.0)) for s, m in zip(grid["store_id"], grid["menu_id"])]
    grid["holiday_factor"] = [holiday_factor_for(hol, s, h, w, params.apply_holiday)
                              for s, h, w in zip(grid["store_id"], grid["is_holiday"], grid["weekday_iso"])]
    _, target_days = prep.promo_days(ds)
    grid["promo_active"] = 0
    if len(target_days):
        td = target_days[target_days["date"] > asof][["store_id", "date", "menu_id"]].drop_duplicates().assign(_p=1)
        grid = grid.merge(td, on=["store_id", "date", "menu_id"], how="left", validate="one_to_one")
        grid["promo_active"] = grid["_p"].fillna(0).astype(int)
        grid = grid.drop(columns="_p")
    grid = grid.merge(promo, on=["store_id", "menu_id"], how="left", validate="many_to_one")
    grid["promo_factor"] = np.where((grid["promo_active"] == 1) & params.apply_promo,
                                    grid["promo_factor"].fillna(1.0), 1.0)
    mm = params.menu_multipliers or {}
    grid["scenario_factor"] = float(params.demand_multiplier) * grid["menu_id"].map(lambda m: float(mm.get(m, 1.0)))
    grid["forecast_qty"] = (grid["base_qty"] * grid["trend_factor"] * grid["holiday_factor"] * grid["promo_factor"]
                            * grid["is_available"] * grid["scenario_factor"]).clip(lower=0.0)
    fc_cols = ["date", "store_id", "menu_id", "weekday_iso", "is_holiday", "holiday_name", "base_qty", "base_source",
               "trend_factor", "holiday_factor", "promo_active", "promo_factor", "is_available", "scenario_factor",
               "forecast_qty"]
    forecast = grid[fc_cols].sort_values(["store_id", "menu_id", "date"]).reset_index(drop=True)
    basis = _finish_basis(ds, basis, forecast, hol, promo, params)
    _note(notes, f"예측 base = 같은 요일 최근 {params.lookback_weeks}회 demand_qty 평균 "
                 f"(공휴일·미취급일·첫 판매 전 날은 건너뛰고 최대 {params.max_lookback_weeks}주 안에서 모음)")
    if params.apply_trend:
        _note(notes, "추세 계수 = 최근 2주/8주 평균 (0.7~1.3 클립) 적용 (백테스트에서는 오차를 키웠음 — 사용자 선택)")
    else:
        _note(notes, "추세 계수 미적용 — 실데이터 백테스트(3~9월)에서 추세 보정이 메뉴·식자재 오차를 모두 키움")
    _note(notes, "공휴일 계수는 매장 단위(메뉴 합계)로 평일 공휴일·주말 공휴일을 나눠 계산 "
                 "(공휴일 실제 ÷ 같은 요일 ±4주 비공휴일 평균)")
    _note(notes, "'취급 중 주문 없음' 구간(첫 판매 전)은 base 에서 제외 (취급표만 믿으면 과소예측)")
    return forecast, basis


def _finish_basis(ds: Dataset, basis: pd.DataFrame, forecast: pd.DataFrame, hol: dict[str, dict[str, Any]],
                  promo: pd.DataFrame, params: EngineParams) -> pd.DataFrame:
    """basis 에 메뉴명·공휴일/프로모션 근거·시나리오 반영 sigma 를 붙인다 (예측표에 있는 매장×메뉴 전부)."""
    pairs = forecast[["store_id", "menu_id"]].drop_duplicates()
    out = pairs.merge(basis, on=["store_id", "menu_id"], how="left", validate="one_to_one")
    out = out.merge(promo, on=["store_id", "menu_id"], how="left", validate="one_to_one")
    out["holiday_factor"] = out["store_id"].map(lambda s: (hol.get(s) or {}).get("factor"))
    out["holiday_factor_weekday"] = out["store_id"].map(lambda s: (hol.get(s) or {}).get("factor_weekday"))
    out["holiday_factor_weekend"] = out["store_id"].map(lambda s: (hol.get(s) or {}).get("factor_weekend"))
    out["holiday_weekend_factor"] = out["store_id"].map(lambda s: (hol.get(s) or {}).get("weekend_factor"))
    out["holiday_note"] = out["store_id"].map(lambda s: (hol.get(s) or {}).get("note", "공휴일 계수 계산 불가 → 1.0"))
    menus = ds.get("02_menus")
    if menus is not None and "menu_name" in menus.columns:
        out = out.merge(menus[["menu_id", "menu_name"]].drop_duplicates("menu_id"), on="menu_id", how="left",
                        validate="many_to_one")
    else:
        out["menu_name"] = out["menu_id"]
    mm = params.menu_multipliers or {}
    scen = out["menu_id"].map(lambda m: float(params.demand_multiplier) * float(mm.get(m, 1.0)))
    out["sigma_raw"] = out["sigma"].fillna(0.0)
    out["sigma"] = out["sigma_raw"] * scen
    out["n_obs"] = out["n_obs"].fillna(0).astype(int)
    out["trend_factor"] = out["trend_factor"].fillna(1.0)
    if "trend_note" in out.columns:
        out["trend_note"] = out["trend_note"].fillna("이력 없음 → 추세 1.0")
    fsum = forecast.groupby(["store_id", "menu_id"], as_index=False)["forecast_qty"].sum().rename(
        columns={"forecast_qty": "forecast_total"})
    return out.merge(fsum, on=["store_id", "menu_id"], how="left", validate="one_to_one")


def basis_values_table(basis_row: pd.Series) -> pd.DataFrame:
    """basis 한 행 → 같은 요일 값 표 (요일, 날짜, 수요, 사용 여부). 설명 패널용."""
    used = pd.DataFrame(basis_row.get("base_values") or [], columns=["date", "weekday_iso", "demand_qty"]).assign(사용="사용")
    exc = pd.DataFrame(basis_row.get("excluded_values") or [], columns=["date", "weekday_iso", "demand_qty", "reason"])
    exc = exc.assign(사용=lambda x: "제외(" + x["reason"].astype(str) + ")").drop(columns="reason")
    t = pd.concat([used, exc], ignore_index=True)
    if t.empty:
        return pd.DataFrame(columns=["요일", "날짜", "수요(개)", "사용"])
    t = t.sort_values(["weekday_iso", "date"])
    return pd.DataFrame({"요일": t["weekday_iso"].map(lambda w: WEEKDAY_KO[int(w) - 1]),
                         "날짜": pd.to_datetime(t["date"]).dt.strftime("%m/%d"),
                         "수요(개)": t["demand_qty"].round(1), "사용": t["사용"]}).reset_index(drop=True)
