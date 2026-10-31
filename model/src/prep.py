"""
발주 엔진이 내일 바로 쓸 전처리 함수 모음 (UI 없음, pandas in/out, ds 의 원본 테이블은 바꾸지 않음).

주요 함수
- daily_sales(ds)                                   : 매장×메뉴×일 판매 합계 (취급일은 0으로 채움)
- daily_ingredient_requirement(ds, sales_measure)   : 매장×식자재×일 소요량 (use_unit, BOM 적용, 세트는 자체 BOM)
- menu_qty_to_ingredients(menu_daily, bom, qty_col) : 메뉴 수량표(예측값 포함) → 식자재 소요량 (엔진 연결 지점)
- price_asof / price_table_asof / attach_price_asof : 기준일 단가 (발주단위당, 사용단위당)
- to_order_units / order_to_use_units / add_order_units : 사용단위 ↔ 발주단위 환산 (최소발주수량 반영)
- SupplierCalendar                                  : 다음 발주 마감 시각, 도착일, 다음 두 번의 도착일
- open_po_lines(ds, asof_date)                      : 기준일까지 미입고 발주 라인 (= 입고 예정 물량)
- lots_asof(ds, asof_date)                          : 기준일 영업 종료 후 로트 목록 + 남은 사용일수
- save_processed(ds, out_dir)                       : 테이블/파생표를 parquet 로 저장

판매 지표 이름 (sales_measure)
- 'demand_qty' : 품절 전 수요 (수요 예측 입력으로 권장)
- 'qty'        : 취소 전 실제 주문 수량
- 'net_qty'    : qty − cancel_qty (취소 반영 판매량)
- 'lost_qty'   : 품절 손실 수량
과거 consumed_qty 재현에 어떤 지표가 맞는지는 validate 의 '소진량 재현' 검사가 알려준다
(fixture 기준 net_qty). 결과를 CONSUMPTION_MEASURE 에 적어 두고 가정표에 기록한다.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional, Union

import numpy as np
import pandas as pd

from . import schema
from .loader import Dataset, MissingDataError, iter_loaded, require

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONSUMPTION_MEASURE = "net_qty"  # validate '소진량 재현' 결과로 확정할 값 (실데이터 확인 후 수정)
SALES_MEASURES = ("demand_qty", "qty", "net_qty", "cancel_qty", "lost_qty")
MEASURE_ALIASES = {"qty-cancel": "net_qty", "qty_net": "net_qty", "net": "net_qty", "qty−cancel_qty": "net_qty",
                   "qty-cancel_qty": "net_qty", "demand": "demand_qty"}
ALL_STORE_TOKENS = {"ALL", "전체", "*", "전매장", "ALL_STORES"}

DateLike = Union[str, pd.Timestamp, "np.datetime64"]


# ---------------------------------------------------------------------------
# 공통 도우미
# ---------------------------------------------------------------------------

def _ts(value: Any) -> pd.Timestamp:
    """날짜/문자열 → 자정 Timestamp."""
    return pd.Timestamp(value).normalize()


def po_line_key(ds: Dataset) -> list[str]:
    """발주 라인을 식별하는 키. po_line_id 가 전체에서 고유하면 ['po_line_id'],
    발주서 안에서만 고유하면 ['po_id', 'po_line_id'] (입고 연결도 이 키로 해야 함)."""
    po = ds.get("11_purchase_orders")
    if po is None or "po_line_id" not in po.columns:
        return ["po_line_id"]
    if not po["po_line_id"].duplicated().any():
        return ["po_line_id"]
    if "po_id" in po.columns and not po.duplicated(["po_id", "po_line_id"]).any():
        return ["po_id", "po_line_id"]
    return ["po_line_id"]


def availability_on(status: Any) -> Optional[bool]:
    """취급 상태 문자열 → True(취급)/False(미취급)/None(해석 불가).

    '미취급', '판매중지', '단종', 'N', '0', 'inactive' → False / '취급', '판매', 'Y', '1', 'active' → True.
    """
    if status is None or (isinstance(status, float) and math.isnan(status)):
        return None
    s = str(status).strip().lower()
    if not s:
        return None
    negative = ("미취급", "미판매", "판매중지", "판매 중지", "중지", "단종", "불가", "inactive", "off", "false", "no")
    if any(tok in s for tok in negative) or s in ("n", "0", "x"):
        return False
    positive = ("취급", "판매", "가능", "active", "on", "true", "yes")
    if any(tok in s for tok in positive) or s in ("y", "1", "o"):
        return True
    return None


def sales_date_range(ds: Dataset) -> pd.DatetimeIndex:
    """판매 데이터가 있는 날짜 범위(최소~최대, 매일). 판매가 없으면 DATA_START~DATA_END."""
    sales = ds.get("09_sales")
    if sales is not None and "date" in sales.columns and sales["date"].notna().any():
        return pd.date_range(sales["date"].min(), sales["date"].max(), freq="D")
    return pd.date_range(schema.DATA_START, schema.DATA_END, freq="D")


def calendar_features(ds: Dataset, dates: pd.DatetimeIndex) -> pd.DataFrame:
    """date, weekday_iso, is_holiday, holiday_name (달력이 없으면 날짜로 요일만 계산)."""
    base = pd.DataFrame({"date": dates})
    cal = ds.get("07_calendar")
    if cal is not None and "date" in cal.columns:
        cols = [c for c in ("date", "weekday_iso", "is_holiday", "holiday_name") if c in cal.columns]
        base = base.merge(cal[cols].drop_duplicates("date"), on="date", how="left", validate="one_to_one")
    if "weekday_iso" not in base.columns:
        base["weekday_iso"] = np.nan
    base["weekday_iso"] = base["weekday_iso"].fillna(base["date"].dt.isocalendar().day.astype(float)).astype(int)
    if "is_holiday" not in base.columns:
        base["is_holiday"] = 0
    base["is_holiday"] = base["is_holiday"].fillna(0).astype(int)
    return base


# ---------------------------------------------------------------------------
# 메뉴 취급 / 프로모션
# ---------------------------------------------------------------------------

def availability_grid(ds: Dataset, dates: Optional[pd.DatetimeIndex] = None,
                      uncovered_policy: str = "unavailable") -> pd.DataFrame:
    """매장×메뉴×일 취급 여부 표 (store_id, menu_id, date, is_available 0/1, availability_source).

    대상 매장×메뉴 = 판매에 나온 조합 ∪ 취급기간표에 나온 조합.
    - 취급기간표의 기간이 그 날짜를 덮으면 그 상태를 따른다 (겹치면 미취급 우선).
    - 표에 조합은 있는데 그 날짜를 덮는 기간이 없으면 uncovered_policy ('unavailable' 기본 / 'available').
    - 표에 조합 자체가 없으면 취급으로 본다.
    - 상태 문자열을 해석 못 하면 취급으로 본다 (availability_on 참고).
    """
    dates = sales_date_range(ds) if dates is None else pd.DatetimeIndex(dates)
    pairs = []
    sales = ds.get("09_sales")
    if sales is not None and {"store_id", "menu_id"} <= set(sales.columns):
        pairs.append(sales[["store_id", "menu_id"]].dropna().drop_duplicates())
    av = ds.get("15_menu_availability")
    av_ok = av is not None and {"store_id", "menu_id", "start_date", "end_date", "status"} <= set(av.columns)
    if av_ok:
        pairs.append(av[["store_id", "menu_id"]].dropna().drop_duplicates())
    if not pairs:
        return pd.DataFrame(columns=["store_id", "menu_id", "date", "is_available", "availability_source"])
    pair_df = pd.concat(pairs, ignore_index=True).drop_duplicates()
    grid = pair_df.merge(pd.DataFrame({"date": dates}), how="cross")
    if not av_ok:
        return grid.assign(is_available=1, availability_source="취급기간표 없음(기본 취급)")
    periods = av[["store_id", "menu_id", "start_date", "end_date", "status"]].copy()
    # 미취급(False)만 0, 취급(True)과 해석 불가(None)는 1
    periods["on"] = periods["status"].map(lambda v: 0.0 if availability_on(v) is False else 1.0)
    m = grid.merge(periods, on=["store_id", "menu_id"], how="inner")
    covers = (m["start_date"].isna() | (m["start_date"] <= m["date"])) & (m["end_date"].isna() | (m["date"] <= m["end_date"]))
    status = (m[covers].groupby(["store_id", "menu_id", "date"], as_index=False)["on"].min())
    out = grid.merge(status, on=["store_id", "menu_id", "date"], how="left", validate="one_to_one")
    listed = periods[["store_id", "menu_id"]].drop_duplicates().assign(_listed=1)
    out = out.merge(listed, on=["store_id", "menu_id"], how="left", validate="many_to_one")
    default = 0 if uncovered_policy == "unavailable" else 1
    out["is_available"] = np.where(out["on"].notna(), out["on"],
                                   np.where(out["_listed"].notna(), default, 1)).astype(int)
    out["availability_source"] = np.where(out["on"].notna(), "취급기간표",
                                          np.where(out["_listed"].notna(), "기간표에 날짜 없음", "기간표에 메뉴 없음(취급)"))
    return out.drop(columns=["on", "_listed"])


def promo_days(ds: Dataset) -> tuple[pd.DataFrame, pd.DataFrame]:
    """프로모션 기간을 날짜로 펼친다.

    반환: (store_days: store_id, date, promo_ids  /  target_days: store_id, date, menu_id, promo_ids)
    store_id 가 'ALL'/'전체'/빈값이면 모든 매장에 적용한다.
    """
    cols_sd = ["store_id", "date", "promo_ids"]
    cols_td = ["store_id", "date", "menu_id", "promo_ids"]
    promos = ds.get("08_promotions")
    if promos is None or not {"store_id", "start_date", "end_date"} <= set(promos.columns):
        return pd.DataFrame(columns=cols_sd), pd.DataFrame(columns=cols_td)
    stores = ds.get("01_stores")
    all_stores = list(stores["store_id"].dropna().unique()) if stores is not None and "store_id" in stores else []
    rows_sd, rows_td = [], []
    for p in promos.itertuples(index=False):
        start, end = getattr(p, "start_date"), getattr(p, "end_date")
        if pd.isna(start):
            continue
        end = schema.DATA_END if pd.isna(end) else end
        sid = getattr(p, "store_id")
        targets = getattr(p, "target_menu", []) if "target_menu" in promos.columns else []
        targets = targets if isinstance(targets, list) else []
        store_list = all_stores if (sid is None or (isinstance(sid, float) and math.isnan(sid))
                                    or str(sid).strip().upper() in ALL_STORE_TOKENS) else [sid]
        pid = getattr(p, "promo_id", "")
        for d in pd.date_range(start, end, freq="D"):
            for s in store_list:
                rows_sd.append((s, d, pid))
                for m in targets:
                    rows_td.append((s, d, m, pid))
    sd = pd.DataFrame(rows_sd, columns=["store_id", "date", "promo_id"])
    td = pd.DataFrame(rows_td, columns=["store_id", "date", "menu_id", "promo_id"])
    sd = (sd.groupby(["store_id", "date"], as_index=False)["promo_id"].agg(lambda x: ",".join(map(str, x)))
          .rename(columns={"promo_id": "promo_ids"})) if len(sd) else pd.DataFrame(columns=cols_sd)
    td = (td.groupby(["store_id", "date", "menu_id"], as_index=False)["promo_id"].agg(lambda x: ",".join(map(str, x)))
          .rename(columns={"promo_id": "promo_ids"})) if len(td) else pd.DataFrame(columns=cols_td)
    return sd, td


# ---------------------------------------------------------------------------
# 판매 / 소요량
# ---------------------------------------------------------------------------

def daily_sales(ds: Dataset, uncovered_policy: str = "unavailable") -> pd.DataFrame:
    """매장×메뉴×일 판매 합계.

    컬럼: date, store_id, menu_id, demand_qty, qty, cancel_qty, lost_qty, sales_amount, net_qty,
          promo_flag(판매 행의 최대값), promo_period(매장·기간 행사 여부), promo_target(대상 메뉴 여부),
          is_available(취급 1/미취급 0), weekday_iso, is_holiday
    - 판매 날짜 범위(최소~최대) 안에서 '취급'인 날은 판매 기록이 없어도 0으로 채운다 (dense).
    - 미취급인 날에 판매가 있으면 버리지 않고 is_available=0 으로 남긴다.
    - 날짜·매장·메뉴가 비어 있는 원본 행은 집계할 수 없어 빠지며, 개수를 result.attrs['n_sales_rows_null_keys'] 에 남긴다.
    """
    sales = require(ds, "09_sales", ["date", "store_id", "menu_id"])
    keys = ["store_id", "menu_id", "date"]
    measures = [c for c in ("demand_qty", "qty", "cancel_qty", "lost_qty", "sales_amount") if c in sales.columns]
    agg: dict[str, Any] = {c: (c, "sum") for c in measures}
    if "promo_flag" in sales.columns:
        agg["promo_flag"] = ("promo_flag", "max")
    agg["n_sales_rows"] = ("date", "size")
    n_null_keys = int(sales[keys].isna().any(axis=1).sum())  # 키 결측 행은 집계 불가 → 개수를 attrs 에 남김
    g = sales.dropna(subset=keys).groupby(keys, as_index=False).agg(**agg)
    grid = availability_grid(ds, sales_date_range(ds), uncovered_policy)[keys + ["is_available"]]
    full = grid.merge(g, on=keys, how="outer", validate="one_to_one", indicator=True)
    full["is_available"] = full["is_available"].fillna(0).astype(int)
    keep = (full["is_available"] == 1) | (full["_merge"] != "left_only")
    out = full[keep].drop(columns="_merge").copy()
    fill_cols = measures + (["promo_flag"] if "promo_flag" in out.columns else []) + ["n_sales_rows"]
    out[fill_cols] = out[fill_cols].fillna(0)
    if {"qty", "cancel_qty"} <= set(out.columns):
        out["net_qty"] = out["qty"] - out["cancel_qty"]
    sd, td = promo_days(ds)
    out = out.merge(sd.assign(promo_period=1)[["store_id", "date", "promo_period"]], on=["store_id", "date"],
                    how="left", validate="many_to_one")
    out = out.merge(td.assign(promo_target=1)[["store_id", "date", "menu_id", "promo_target"]], on=keys,
                    how="left", validate="one_to_one")
    out[["promo_period", "promo_target"]] = out[["promo_period", "promo_target"]].fillna(0).astype(int)
    cal = calendar_features(ds, pd.DatetimeIndex(sorted(out["date"].dropna().unique())))
    out = out.merge(cal[["date", "weekday_iso", "is_holiday"]], on="date", how="left", validate="many_to_one")
    order = ["date", "store_id", "menu_id"] + [c for c in out.columns if c not in ("date", "store_id", "menu_id")]
    result = out[order].sort_values(["date", "store_id", "menu_id"]).reset_index(drop=True)
    result.attrs["n_sales_rows_null_keys"] = n_null_keys  # 날짜·매장·메뉴가 비어 집계에서 빠진 원본 행 수
    return result


def menu_qty_to_ingredients(menu_daily: pd.DataFrame, bom: pd.DataFrame, qty_col: str,
                            keys: Iterable[str] = ("date", "store_id")) -> pd.DataFrame:
    """메뉴 수량표 → 식자재 소요량표 (BOM 적용). 예측 수요표를 넣으면 예측 소요량이 나온다.

    menu_daily: keys + ['menu_id', qty_col] 을 가진 표 (예: daily_sales 결과, 예측 결과)
    bom       : 04_bom (menu_id, ingredient_id, qty_per_menu). 세트 메뉴는 세트 자체 BOM 행을 쓴다(구성품 전개 안 함).
    반환      : keys + ['ingredient_id', 'requirement_qty'] (use_unit)
    BOM 이 없는 메뉴는 소요량이 0으로 빠지므로 validate 의 참조 무결성/세트 검사를 함께 볼 것.
    """
    keys = list(keys)
    need = menu_daily[keys + ["menu_id", qty_col]].merge(
        bom[["menu_id", "ingredient_id", "qty_per_menu"]], on="menu_id", how="inner", validate="many_to_many")
    need["requirement_qty"] = need[qty_col].astype(float) * need["qty_per_menu"].astype(float)
    return need.groupby(keys + ["ingredient_id"], as_index=False)["requirement_qty"].sum()


def daily_ingredient_requirement(ds: Dataset, sales_measure: str = "demand_qty",
                                 menu_daily: Optional[pd.DataFrame] = None) -> pd.DataFrame:
    """매장×식자재×일 소요량 (use_unit).

    sales_measure: 'demand_qty'(기본, 예측용) / 'qty' / 'net_qty'(= qty − cancel_qty) / 'lost_qty'
    menu_daily   : 미리 만든 daily_sales 결과 또는 예측 수요표 (없으면 daily_sales(ds) 사용)
    반환 컬럼     : date, store_id, ingredient_id, requirement_qty, use_unit, sales_measure
    """
    measure = MEASURE_ALIASES.get(sales_measure, sales_measure)
    md = daily_sales(ds) if menu_daily is None else menu_daily
    if measure not in md.columns:
        raise ValueError(f"판매 지표 '{measure}' 가 없음. 가능한 값: {[c for c in SALES_MEASURES if c in md.columns]}")
    bom = require(ds, "04_bom", ["menu_id", "ingredient_id", "qty_per_menu"])
    req = menu_qty_to_ingredients(md, bom, measure)
    ing = ds.get("03_ingredients")
    if ing is not None and "use_unit" in ing.columns:
        req = req.merge(ing[["ingredient_id", "use_unit"]].drop_duplicates("ingredient_id"), on="ingredient_id",
                        how="left", validate="many_to_one")
    req["sales_measure"] = measure
    return req.sort_values(["date", "store_id", "ingredient_id"]).reset_index(drop=True)


# ---------------------------------------------------------------------------
# 단가
# ---------------------------------------------------------------------------

def _price_history(ds: Dataset) -> pd.DataFrame:
    ph = require(ds, "06_ingredient_price_history", ["ingredient_id", "valid_from", "unit_price"])
    return ph.dropna(subset=["ingredient_id", "valid_from"]).sort_values(["ingredient_id", "valid_from"])


def price_asof(ds: Dataset, ingredient_id: str, date: DateLike) -> float:
    """date 시점의 발주단위당 단가 (valid_from ≤ date 인 마지막 행). 없으면 NaN."""
    ph = _price_history(ds)
    rows = ph[(ph["ingredient_id"] == ingredient_id) & (ph["valid_from"] <= _ts(date))]
    return float(rows["unit_price"].iloc[-1]) if len(rows) else float("nan")


def price_table_asof(ds: Dataset, date: DateLike) -> pd.DataFrame:
    """모든 식자재의 date 시점 단가표.

    컬럼: ingredient_id, valid_from, unit_price(원/발주단위), use_qty_per_order_unit,
          unit_price_per_use_unit(원/사용단위), order_unit, use_unit
    단가가 없는 식자재도 행은 남기고 단가는 NaN.
    """
    ph = _price_history(ds)
    cur = ph[ph["valid_from"] <= _ts(date)].groupby("ingredient_id", as_index=False).last()
    ing = ds.get("03_ingredients")
    if ing is None:
        return cur
    cols = [c for c in ("ingredient_id", "use_qty_per_order_unit", "order_unit", "use_unit") if c in ing.columns]
    out = ing[cols].drop_duplicates("ingredient_id").merge(cur[["ingredient_id", "valid_from", "unit_price"]],
                                                           on="ingredient_id", how="left", validate="one_to_one")
    if "use_qty_per_order_unit" in out.columns:
        out["unit_price_per_use_unit"] = out["unit_price"] / out["use_qty_per_order_unit"]
    return out


def attach_price_asof(ds: Dataset, df: pd.DataFrame, date_col: str, ingredient_col: str = "ingredient_id",
                      out_col: str = "unit_price_asof") -> pd.DataFrame:
    """df 의 각 행에 date_col 시점 단가(발주단위당)를 붙인 새 DataFrame (행 순서 유지)."""
    ph = _price_history(ds)[["ingredient_id", "valid_from", "unit_price"]]
    left = df.reset_index(drop=True).assign(_row=lambda x: np.arange(len(x)))
    ok = left[date_col].notna() & left[ingredient_col].notna()
    part = left[ok].sort_values(date_col)
    right = ph.rename(columns={"ingredient_id": ingredient_col, "unit_price": out_col}).sort_values("valid_from")
    merged = pd.merge_asof(part, right, left_on=date_col, right_on="valid_from", by=ingredient_col,
                           direction="backward")
    merged = merged.drop(columns=["valid_from"])
    rest = left[~ok].assign(**{out_col: np.nan})
    out = pd.concat([merged, rest], ignore_index=True).sort_values("_row").drop(columns="_row")
    return out.reset_index(drop=True)


# ---------------------------------------------------------------------------
# 단위 환산
# ---------------------------------------------------------------------------

def _ingredient_row(ds: Dataset, ingredient_id: str) -> pd.Series:
    ing = require(ds, "03_ingredients", ["ingredient_id", "use_qty_per_order_unit"])
    rows = ing[ing["ingredient_id"] == ingredient_id]
    if rows.empty:
        raise KeyError(f"식자재 없음: {ingredient_id}")
    return rows.iloc[0]


def to_order_units(ds: Dataset, ingredient_id: str, use_qty: float) -> tuple[int, float]:
    """필요 사용단위 수량 → (발주단위 수량[올림, 최소발주수량 반영], 그만큼의 사용단위 수량).

    예: 양상추 필요 4,300 g, 박스당 2,000 g, 최소 1박스 → (3, 6000.0)
    필요량이 0 이하이면 (0, 0.0). 최소발주수량은 발주가 있을 때만 적용한다.
    """
    row = _ingredient_row(ds, ingredient_id)
    conv = float(row["use_qty_per_order_unit"])
    if not use_qty or pd.isna(use_qty) or use_qty <= 0:
        return 0, 0.0
    if not conv or pd.isna(conv) or conv <= 0:
        raise ValueError(f"{ingredient_id}: use_qty_per_order_unit 이 잘못됨 ({conv})")
    n = math.ceil(round(float(use_qty) / conv, 9))
    moq = row.get("min_order_qty", np.nan)
    if pd.notna(moq) and moq > 0:
        n = max(n, int(math.ceil(float(moq))))
    return int(n), float(n * conv)


def order_to_use_units(ds: Dataset, ingredient_id: str, order_qty: float) -> float:
    """발주단위 수량 → 사용단위 수량 (order_qty × use_qty_per_order_unit)."""
    row = _ingredient_row(ds, ingredient_id)
    return float(order_qty) * float(row["use_qty_per_order_unit"])


def add_order_units(ds: Dataset, df: pd.DataFrame, use_qty_col: str, ingredient_col: str = "ingredient_id") -> pd.DataFrame:
    """df 의 필요 사용단위 수량(use_qty_col)에 대해 발주단위 수량을 붙인 새 표 (벡터화 버전).

    추가 컬럼: use_qty_per_order_unit, min_order_qty, order_qty(올림+최소발주), order_use_qty(발주량의 사용단위 환산)
    """
    ing = require(ds, "03_ingredients", ["ingredient_id", "use_qty_per_order_unit"])
    cols = ["ingredient_id", "use_qty_per_order_unit"] + (["min_order_qty"] if "min_order_qty" in ing.columns else [])
    m = df.merge(ing[cols].drop_duplicates("ingredient_id").rename(columns={"ingredient_id": ingredient_col}),
                 on=ingredient_col, how="left", validate="many_to_one")
    need = m[use_qty_col].astype(float)
    conv = m["use_qty_per_order_unit"].astype(float)
    raw = np.ceil(np.round(need / conv, 9))
    moq = m["min_order_qty"].astype(float).fillna(0) if "min_order_qty" in m.columns else 0.0
    order = np.where(need > 0, np.maximum(raw, np.ceil(moq)), 0.0)
    m["order_qty"] = pd.Series(order, index=m.index).fillna(0).astype(int)
    m["order_use_qty"] = m["order_qty"] * conv
    return m


# ---------------------------------------------------------------------------
# 공급사 달력
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class SupplierTerms:
    supplier_id: str
    name: str
    order_days: tuple[int, ...]       # ISO 1=월 … 7=일
    delivery_days: tuple[int, ...]
    cutoff_min: Optional[int]         # 자정 기준 분. None 이면 23:59 로 본다
    lead_time_days: int
    min_order_amount: float


@dataclass(frozen=True)
class OrderPlan:
    """다음 두 번의 발주 기회와 도착일 (설명 패널용)."""

    supplier_id: str
    asof: pd.Timestamp
    order1_deadline: pd.Timestamp
    arrival1: pd.Timestamp
    order2_deadline: pd.Timestamp
    arrival2: pd.Timestamp

    @property
    def gap_days(self) -> int:
        """이번 발주분이 버텨야 하는 일수 = 두 번째 도착일 − 첫 도착일."""
        return int((self.arrival2 - self.arrival1).days)

    def explain(self) -> str:
        wd = "월화수목금토일"
        f = lambda t: f"{t:%m/%d}({wd[t.isoweekday() - 1]})"  # noqa: E731
        return (f"다음 발주 마감 {f(self.order1_deadline)} {self.order1_deadline:%H:%M} → 도착 {f(self.arrival1)}, "
                f"그 다음 발주 {f(self.order2_deadline)} → 도착 {f(self.arrival2)} (공백 {self.gap_days}일)")


class SupplierCalendar:
    """공급사 발주/입고 요일·마감·리드타임 규칙.

    규칙 (validate '입고 예정일 규칙' 검사로 실데이터에서 확인할 것)
    - 발주 기회: order_days 에 속한 날의 마감(order_cutoff) 전. 오늘이 발주일이고 지금 < 마감이면 오늘.
    - 도착일: 발주일 + lead_time_days, 그날이 delivery_days 가 아니면 다음 입고 가능 요일로 민다.
    - closed_dates: 공급사 휴무일(공휴일 등)을 넣으면 발주/입고 모두 건너뛴다 (기본 없음).
    요일 정보가 비어 있으면 '매일 가능'으로 보고 warnings 에 남긴다.
    """

    MAX_SEARCH_DAYS = 28

    def __init__(self, terms: dict[str, SupplierTerms], closed_dates: Iterable[DateLike] = (),
                 warnings: Optional[list[str]] = None) -> None:
        if not isinstance(terms, dict):  # Dataset 을 그대로 넘기는 실수 방지
            raise TypeError("SupplierCalendar(terms) 는 {supplier_id: SupplierTerms} dict 를 받습니다. "
                            "Dataset 에서 만들 때는 SupplierCalendar.from_dataset(ds) 를 쓰세요.")
        self._terms = terms
        self.closed = {_ts(d) for d in closed_dates}
        self.warnings = list(warnings or [])

    @classmethod
    def from_dataset(cls, ds: Dataset, closed_dates: Iterable[DateLike] = ()) -> "SupplierCalendar":
        sup = require(ds, "05_suppliers", ["supplier_id"])
        terms: dict[str, SupplierTerms] = {}
        warns: list[str] = []
        for r in sup.to_dict("records"):
            sid = r["supplier_id"]
            od = tuple(int(x) for x in (r.get("order_days") or []) if pd.notna(x))
            dd = tuple(int(x) for x in (r.get("delivery_days") or []) if pd.notna(x))
            if not od:
                warns.append(f"{sid}: order_days 비어 있음 → 매일 발주 가능으로 가정")
                od = (1, 2, 3, 4, 5, 6, 7)
            if not dd:
                warns.append(f"{sid}: delivery_days 비어 있음 → 매일 입고 가능으로 가정")
                dd = (1, 2, 3, 4, 5, 6, 7)
            cut = r.get("order_cutoff_min")
            if cut is None or pd.isna(cut):
                warns.append(f"{sid}: order_cutoff 없음 → 23:59 마감으로 가정")
                cut = None
            lead = r.get("lead_time_days")
            if lead is None or pd.isna(lead):
                warns.append(f"{sid}: lead_time_days 없음 → 0일로 가정")
                lead = 0
            moa = r.get("min_order_amount")
            terms[sid] = SupplierTerms(sid, str(r.get("supplier_name", "")), od, dd,
                                       None if cut is None else int(cut), int(lead),
                                       0.0 if moa is None or pd.isna(moa) else float(moa))
        return cls(terms, closed_dates, warns)

    @property
    def supplier_ids(self) -> list[str]:
        return list(self._terms)

    def terms(self, supplier_id: str) -> SupplierTerms:
        if supplier_id not in self._terms:
            raise KeyError(f"공급사 없음: {supplier_id}")
        return self._terms[supplier_id]

    def is_order_day(self, supplier_id: str, date: DateLike) -> bool:
        d = _ts(date)
        return d.isoweekday() in self.terms(supplier_id).order_days and d not in self.closed

    def is_delivery_day(self, supplier_id: str, date: DateLike) -> bool:
        d = _ts(date)
        return d.isoweekday() in self.terms(supplier_id).delivery_days and d not in self.closed

    def next_order_datetime(self, supplier_id: str, asof_datetime: DateLike) -> pd.Timestamp:
        """asof 이후 가장 빠른 발주 기회의 '마감 시각' (날짜 + order_cutoff). 날짜만 필요하면 .normalize().

        asof 에 시각이 없으면 00:00 으로 본다 → 오늘이 발주일이면 오늘 마감이 첫 기회.
        """
        t = self.terms(supplier_id)
        asof = pd.Timestamp(asof_datetime)
        cutoff = t.cutoff_min if t.cutoff_min is not None else 24 * 60 - 1
        day0 = asof.normalize()
        for k in range(self.MAX_SEARCH_DAYS):
            d = day0 + pd.Timedelta(days=k)
            if self.is_order_day(supplier_id, d):
                deadline = d + pd.Timedelta(minutes=cutoff)
                if asof < deadline:
                    return deadline
        raise ValueError(f"{supplier_id}: {self.MAX_SEARCH_DAYS}일 안에 발주 가능일이 없음")

    def arrival_date(self, supplier_id: str, order_date: DateLike) -> pd.Timestamp:
        """발주일 + lead_time_days 를 입고 가능 요일까지 뒤로 민 날."""
        t = self.terms(supplier_id)
        d = _ts(order_date) + pd.Timedelta(days=t.lead_time_days)
        for k in range(self.MAX_SEARCH_DAYS):
            cand = d + pd.Timedelta(days=k)
            if self.is_delivery_day(supplier_id, cand):
                return cand
        raise ValueError(f"{supplier_id}: {self.MAX_SEARCH_DAYS}일 안에 입고 가능일이 없음")

    def order_plan(self, supplier_id: str, asof_datetime: DateLike) -> OrderPlan:
        """다음 발주(→도착1)와, 도착1 보다 늦게 도착하는 그 다음 발주(→도착2)."""
        o1 = self.next_order_datetime(supplier_id, asof_datetime)
        a1 = self.arrival_date(supplier_id, o1)
        o2 = o1
        for _ in range(self.MAX_SEARCH_DAYS):
            o2 = self.next_order_datetime(supplier_id, o2 + pd.Timedelta(minutes=1))
            a2 = self.arrival_date(supplier_id, o2)
            if a2 > a1:
                return OrderPlan(supplier_id, pd.Timestamp(asof_datetime), o1, a1, o2, a2)
        raise ValueError(f"{supplier_id}: 두 번째 도착일을 찾지 못함")

    def next_two_arrivals(self, supplier_id: str, asof_datetime: DateLike) -> tuple[pd.Timestamp, pd.Timestamp]:
        """(이번 발주 도착일, 다음 발주 도착일). 이번 발주량은 두 도착일 사이 공백을 버텨야 한다."""
        plan = self.order_plan(supplier_id, asof_datetime)
        return plan.arrival1, plan.arrival2


# ---------------------------------------------------------------------------
# 미입고 발주 / 로트
# ---------------------------------------------------------------------------

def open_po_lines(ds: Dataset, asof_date: DateLike, partial_policy: str = "closed") -> pd.DataFrame:
    """asof_date(영업 종료 시점)까지 발주했지만 입고되지 않은 발주 라인 = 입고 예정 물량.

    partial_policy:
      'closed'        (기본) 입고 기록이 하나라도 있으면 끝난 라인으로 본다 (부분입고 잔량은 안 온다고 가정)
      'open_remainder' 주문량 − 누적 입고량 이 남으면 그만큼 열린 라인으로 본다
    반환 컬럼: 발주 컬럼 + received_qty_to_date, n_receipts, open_order_qty, use_qty_per_order_unit,
              open_use_qty(사용단위), use_unit, days_until_expected, is_overdue(예정일 ≤ 기준일인데 미입고)
    """
    po = require(ds, "11_purchase_orders", ["po_line_id", "order_date", "ingredient_id", "order_qty"])
    asof = _ts(asof_date)
    key = po_line_key(ds)
    lines = po[po["order_date"] <= asof].copy()
    rc = ds.get("12_receipts")
    if rc is not None and set(key + ["receipt_date", "received_qty"]) <= set(rc.columns):
        got = (rc[rc["receipt_date"] <= asof].groupby(key, as_index=False)
               .agg(received_qty_to_date=("received_qty", "sum"), n_receipts=("receipt_date", "size")))
        lines = lines.merge(got, on=key, how="left", validate="many_to_one")
    else:
        lines["received_qty_to_date"] = np.nan
        lines["n_receipts"] = np.nan
    lines["received_qty_to_date"] = lines["received_qty_to_date"].fillna(0.0)
    lines["n_receipts"] = lines["n_receipts"].fillna(0).astype(int)
    if partial_policy == "open_remainder":
        lines["open_order_qty"] = (lines["order_qty"] - lines["received_qty_to_date"]).clip(lower=0)
        out = lines[lines["open_order_qty"] > 1e-9].copy()
    else:
        out = lines[lines["n_receipts"] == 0].copy()
        out["open_order_qty"] = out["order_qty"]
    ing = ds.get("03_ingredients")
    if ing is not None and "use_qty_per_order_unit" in ing.columns:
        cols = [c for c in ("ingredient_id", "use_qty_per_order_unit", "use_unit") if c in ing.columns]
        out = out.merge(ing[cols].drop_duplicates("ingredient_id"), on="ingredient_id", how="left",
                        validate="many_to_one")
        out["open_use_qty"] = out["open_order_qty"] * out["use_qty_per_order_unit"]
    if "expected_date" in out.columns:
        out["days_until_expected"] = (out["expected_date"] - asof).dt.days
        out["is_overdue"] = out["expected_date"] <= asof
    return out.sort_values([c for c in ("expected_date", "store_id", "ingredient_id") if c in out.columns]).reset_index(drop=True)


def lots_asof(ds: Dataset, asof_date: DateLike) -> pd.DataFrame:
    """asof_date 영업 종료 후 로트 목록.

    추가 컬럼: days_to_expiry = last_use_date − asof_date (일). 내일부터 쓸 수 있는 남은 일수와 같다.
              (0 이하 = 내일은 못 씀 → 폐기 예정), shelf_life_days, storage, unit_price_per_use_unit, lot_value
    해당 날짜 로트가 없으면 빈 표.
    """
    lots = require(ds, "16_inventory_lots_daily", ["date", "store_id", "ingredient_id", "lot_id", "remaining_qty"])
    d = _ts(asof_date)
    out = lots[lots["date"] == d].copy()
    if "last_use_date" in out.columns:
        out["days_to_expiry"] = (out["last_use_date"] - d).dt.days
        out["expires_before_next_day"] = out["days_to_expiry"] <= 0
    ing = ds.get("03_ingredients")
    if ing is not None:
        cols = [c for c in ("ingredient_id", "shelf_life_days", "storage") if c in ing.columns]
        out = out.merge(ing[cols].drop_duplicates("ingredient_id"), on="ingredient_id", how="left", validate="many_to_one")
    if ds.get("06_ingredient_price_history") is not None and ds.get("03_ingredients") is not None:
        try:
            prices = price_table_asof(ds, d)[["ingredient_id", "unit_price_per_use_unit"]]
            out = out.merge(prices, on="ingredient_id", how="left", validate="many_to_one")
            out["lot_value"] = out["remaining_qty"] * out["unit_price_per_use_unit"]
        except (MissingDataError, KeyError):
            pass
    sort_cols = [c for c in ("store_id", "ingredient_id", "last_use_date", "lot_id") if c in out.columns]
    return out.sort_values(sort_cols).reset_index(drop=True)


def default_asof(ds: Dataset) -> pd.Timestamp:
    """기본 기준일: 일별 재고의 마지막 날짜 (없으면 DATA_END)."""
    inv = ds.get("10_inventory_daily")
    if inv is not None and "date" in inv.columns and inv["date"].notna().any():
        return min(_ts(inv["date"].max()), schema.DATA_END)
    return schema.DATA_END


# ---------------------------------------------------------------------------
# 저장
# ---------------------------------------------------------------------------

def _to_parquet(df: pd.DataFrame, path: Path) -> str:
    """parquet 저장. 목록 컬럼 등으로 실패하면 object 컬럼을 JSON 문자열로 바꿔 재시도. 메모를 돌려준다."""
    try:
        df.to_parquet(path, engine="pyarrow", index=False)
        return "ok"
    except Exception as first:  # pyarrow 형 변환 실패 → 문자열로 저장
        safe = df.copy()
        for c in safe.columns:
            if safe[c].dtype == object:
                safe[c] = safe[c].map(lambda v: json.dumps(v, ensure_ascii=False, default=str)
                                      if isinstance(v, (list, dict, tuple)) else (None if v is None else str(v)))
        safe.to_parquet(path, engine="pyarrow", index=False)
        return f"object 컬럼을 문자열로 바꿔 저장 ({type(first).__name__})"


def save_processed(ds: Dataset, out_dir: Union[str, Path] = PROJECT_ROOT / "data" / "processed") -> dict[str, Any]:
    """테이블별 parquet(tables/<key>.parquet)와 파생표를 저장하고 manifest.json 을 쓴다.

    파생표: daily_sales, requirement_demand_qty, requirement_<CONSUMPTION_MEASURE>, open_po_asof, lots_asof
    반환: {'written': {이름: 경로}, 'notes': {이름: 메모}, 'errors': {이름: 오류}}
    """
    out = Path(out_dir)
    (out / "tables").mkdir(parents=True, exist_ok=True)
    written: dict[str, str] = {}
    notes: dict[str, str] = {}
    errors: dict[str, str] = {}

    def save(name: str, frame_fn: Any, path: Path) -> None:
        try:
            frame = frame_fn()
            note = _to_parquet(frame, path)
            written[name] = str(path)
            notes[name] = f"{len(frame):,}행, {note}"
        except Exception as e:  # 한 파생표 실패가 나머지 저장을 막지 않게
            errors[name] = f"{type(e).__name__}: {e}"

    for key, df in iter_loaded(ds):
        save(f"tables/{key}", lambda df=df: df, out / "tables" / f"{key}.parquet")
    asof = default_asof(ds)
    ds_cache: dict[str, pd.DataFrame] = {}

    def cached_daily() -> pd.DataFrame:
        if "daily" not in ds_cache:
            ds_cache["daily"] = daily_sales(ds)
        return ds_cache["daily"]

    save("daily_sales", cached_daily, out / "daily_sales.parquet")
    for measure in dict.fromkeys(["demand_qty", CONSUMPTION_MEASURE]):
        save(f"requirement_{measure}",
             lambda m=measure: daily_ingredient_requirement(ds, m, menu_daily=cached_daily()),
             out / f"requirement_{measure}.parquet")
    save("open_po_asof", lambda: open_po_lines(ds, asof), out / "open_po_asof.parquet")
    save("lots_asof", lambda: lots_asof(ds, asof), out / "lots_asof.parquet")
    manifest = {"source": ds.source, "data_dir": str(ds.data_dir), "asof": str(asof.date()),
                "consumption_measure": CONSUMPTION_MEASURE, "written": written, "notes": notes, "errors": errors}
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"written": written, "notes": notes, "errors": errors, "asof": asof}
