"""
명세서(docs/data_spec.md) 형식을 그대로 따르는 작은 합성 데이터셋을 만든다 (기본: data/fixture/).

목적: 실제 CSV 가 오기 전에 loader → profile → validate → prep 전체를 끝까지 돌려 보기 위한
'정답이 알려진' 데이터. 아래 성질이 성립하도록 하루씩, 시간대별로 시뮬레이션한다.
  - 소진량 consumed = Σ (qty − cancel_qty) × BOM   (세트는 세트 자체 BOM 사용)
  - demand_qty = qty + lost_qty, sales_amount = (qty − cancel_qty) × virtual_price
  - 재고 항등식 closing = opening + received + adjust + transfer_in − consumed − waste − transfer_out,
    counted = opening + adjust, opening(t) = closing(t−1)
  - 로트 잔량 합 = closing, last_use_date = receipt_date + shelf_life_days − 1 (FEFO 소진)
  - 입고 received_use_qty = received_qty × use_qty_per_order_unit, delay_days = 입고일 − 예정일
  - 일별 재고의 입고/폐기/이동 = 12/13/14 테이블 합계
  - avg_consumption_7d = 당일 포함 최근 7일 평균 (처음 6일은 있는 날만 평균)
  - low_stock_flag: 커버일수(closing / avg7) < 1.5, overstock_flag: 커버일수 > 7 (avg7=0 이고 재고>0 도 과잉)
구성: 매장 3, 메뉴 12(세트 2), 식자재 15, 공급사 3, 기간 2026-08-02 ~ 2026-09-30 (60일),
      프로모션 3, 미취급 기간 2, 단가 변경 1, 지연 입고 1, 부분 입고 1, 기간 말 미입고 발주, 매장 간 이동 3,
      발주 누락으로 인한 품절과 긴급발주, 달력은 2026-01-01 ~ 2026-10-31 (10월 공휴일 포함).
표기: 요일은 한글(월,화,…), 목록은 쉼표 구분(따옴표로 감쌈), UTF-8, 날짜 YYYY-MM-DD.
주의: 공휴일 목록은 합성용이다. 실제 판단은 제공 달력(07_calendar)을 따른다.

실행: python tests/make_fixture.py [--out data/fixture] [--seed 7]
"""
from __future__ import annotations

import argparse
import math
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = PROJECT_ROOT / "data" / "fixture"

START = date(2026, 8, 2)
END = date(2026, 9, 30)
CAL_START = date(2026, 1, 1)
CAL_END = date(2026, 10, 31)
KOR_DAYS = "월화수목금토일"  # isoweekday 1..7

# ---------------------------------------------------------------------------
# 마스터 데이터
# ---------------------------------------------------------------------------
STORES = [
    dict(store_id="S01", store_name="강남역점", trade_area="오피스", store_type="일반매장",
         open_hour=10, close_hour=22, dine_in="Y", delivery="Y", relative_size=1.0),
    dict(store_id="S02", store_name="목동점", trade_area="주거", store_type="일반매장",
         open_hour=10, close_hour=22, dine_in="Y", delivery="Y", relative_size=0.8),
    dict(store_id="S03", store_name="신촌점", trade_area="대학가", store_type="배달전문",
         open_hour=11, close_hour=23, dine_in="N", delivery="Y", relative_size=1.3),
]
STORE_IDS = [s["store_id"] for s in STORES]
CHANNEL_SHARE = {
    "S01": {"매장": 0.55, "포장": 0.25, "배달": 0.20},
    "S02": {"매장": 0.45, "포장": 0.25, "배달": 0.30},
    "S03": {"포장": 0.30, "배달": 0.70},
}
CANCEL_P = {"매장": 0.01, "포장": 0.02, "배달": 0.04}
WEEKDAY_FACTOR = {  # isoweekday → 배율
    "S01": {1: 1.1, 2: 1.1, 3: 1.1, 4: 1.1, 5: 1.1, 6: 0.75, 7: 0.65},
    "S02": {1: 0.9, 2: 0.9, 3: 0.9, 4: 0.9, 5: 0.9, 6: 1.3, 7: 1.25},
    "S03": {1: 1.0, 2: 1.0, 3: 1.0, 4: 1.0, 5: 1.2, 6: 1.1, 7: 0.9},
}
HOLIDAY_FACTOR = {"S01": 0.5, "S02": 1.2, "S03": 0.6}

# menu_id, 이름, 분류, 세트여부, 구성, 가격, S01 기준 일 평균 수요
MENUS = [
    ("M01", "불고기버거", "버거", 0, "", 5500, 60),
    ("M02", "치즈버거", "버거", 0, "", 5000, 45),
    ("M03", "치킨버거", "버거", 0, "", 5800, 50),
    ("M04", "새우버거", "버거", 0, "", 5900, 30),
    ("M05", "더블치즈버거", "버거", 0, "", 7500, 25),
    ("M06", "감자튀김", "사이드", 0, "", 2500, 70),
    ("M07", "그린샐러드", "사이드", 0, "", 4500, 15),
    ("M08", "콜라", "음료", 0, "", 2000, 60),
    ("M09", "사이다", "음료", 0, "", 2000, 30),
    ("M10", "토마토버거", "버거", 0, "", 6200, 20),
    ("M11", "불고기세트", "세트", 1, "M01,M06,M08", 8500, 55),
    ("M12", "치킨버거세트", "세트", 1, "M03,M06,M09", 8800, 40),
]
MENU_IDS = [m[0] for m in MENUS]
MENU_PRICE = {m[0]: m[5] for m in MENUS}
MENU_BASE = {m[0]: m[6] for m in MENUS}

# ingredient_id, 이름, 공급사, 사용단위, 발주단위, 환산, 보관, 사용기한(입고일 포함), 최소발주, 기본단가
INGREDIENTS = [
    ("I01", "소고기패티", "SP01", "개", "박스", 100, "냉동", 90, 1, 65000),
    ("I02", "치킨패티", "SP01", "개", "박스", 80, "냉동", 90, 1, 52000),
    ("I03", "새우패티", "SP01", "개", "박스", 50, "냉동", 90, 1, 60000),
    ("I04", "버거번", "SP02", "개", "트레이", 48, "상온", 4, 2, 19200),
    ("I05", "치즈슬라이스", "SP02", "매", "팩", 100, "냉장", 30, 1, 18000),
    ("I06", "양상추", "SP02", "g", "박스", 2000, "냉장", 5, 1, 9000),
    ("I07", "토마토", "SP02", "g", "박스", 3000, "냉장", 7, 1, 12000),
    ("I08", "양파", "SP02", "g", "박스", 5000, "냉장", 14, 1, 8000),
    ("I09", "피클", "SP02", "g", "통", 3000, "냉장", 60, 1, 9000),
    ("I10", "버거소스", "SP03", "g", "통", 2000, "냉장", 30, 1, 11000),
    ("I11", "감자튀김", "SP01", "g", "박스", 10000, "냉동", 120, 1, 32000),
    ("I12", "콜라시럽", "SP03", "ml", "BOX", 18000, "상온", 180, 1, 45000),
    ("I13", "사이다시럽", "SP03", "ml", "BOX", 18000, "상온", 180, 1, 45000),
    ("I14", "컵", "SP03", "개", "BOX", 1000, "상온", 365, 1, 40000),
    ("I15", "포장지", "SP03", "장", "팩", 500, "상온", 365, 1, 15000),
]
ING = {r[0]: dict(name=r[1], supplier=r[2], use_unit=r[3], order_unit=r[4], conv=r[5], storage=r[6],
                  shelf=r[7], moq=r[8], price=r[9]) for r in INGREDIENTS}
ING_IDS = [r[0] for r in INGREDIENTS]
PRICE_CHANGES = {"I06": [(date(2026, 9, 1), 10500)]}  # 양상추 9월 단가 인상

BOM: dict[str, dict[str, int]] = {
    "M01": {"I04": 1, "I01": 1, "I06": 20, "I08": 15, "I10": 20, "I15": 1},
    "M02": {"I04": 1, "I01": 1, "I05": 1, "I09": 10, "I10": 15, "I15": 1},
    "M03": {"I04": 1, "I02": 1, "I06": 25, "I10": 20, "I15": 1},
    "M04": {"I04": 1, "I03": 1, "I06": 20, "I10": 20, "I15": 1},
    "M05": {"I04": 1, "I01": 2, "I05": 2, "I09": 15, "I08": 10, "I10": 20, "I15": 1},
    "M06": {"I11": 150},
    "M07": {"I06": 80, "I07": 50, "I08": 20, "I10": 15},
    "M08": {"I12": 60, "I14": 1},
    "M09": {"I13": 60, "I14": 1},
    "M10": {"I04": 1, "I01": 1, "I07": 40, "I06": 20, "I10": 20, "I15": 1},
    # 세트: 자체 BOM. M11 은 구성 메뉴 BOM 합과 같고, M12 는 감자튀김을 120g 만 쓴다 (합과 다름).
    "M11": {"I04": 1, "I01": 1, "I06": 20, "I08": 15, "I10": 20, "I15": 1, "I11": 150, "I12": 60, "I14": 1},
    "M12": {"I04": 1, "I02": 1, "I06": 25, "I10": 20, "I15": 1, "I11": 120, "I13": 60, "I14": 1},
}

SUPPLIERS = [
    dict(supplier_id="SP01", supplier_name="한빛냉동식품", order_days="월,목", order_cutoff="14:00",
         lead_time_days=2, delivery_days="수,토", min_order_amount=100000,
         note="냉동 차량 배송. 긴급발주 시 익일 입고(단가 10% 할증)"),
    dict(supplier_id="SP02", supplier_name="새벽신선유통", order_days="월,화,수,목,금,토", order_cutoff="11:00",
         lead_time_days=1, delivery_days="화,수,목,금,토", min_order_amount=50000,
         note="일·월 배송 없음. 토요일 발주분은 화요일 입고"),
    dict(supplier_id="SP03", supplier_name="푸드패키지", order_days="화,금", order_cutoff="16:00",
         lead_time_days=3, delivery_days="월,금", min_order_amount=0, note="최소 발주금액 없음"),
]
SUP = {s["supplier_id"]: s for s in SUPPLIERS}


def _days(text: str) -> set[int]:
    return {KOR_DAYS.index(t) + 1 for t in text.split(",")}


SUP_ORDER_DAYS = {s["supplier_id"]: _days(s["order_days"]) for s in SUPPLIERS}
SUP_DELIVERY_DAYS = {s["supplier_id"]: _days(s["delivery_days"]) for s in SUPPLIERS}
SUP_CUTOFF_MIN = {s["supplier_id"]: int(s["order_cutoff"][:2]) * 60 + int(s["order_cutoff"][3:]) for s in SUPPLIERS}
INITIAL_STOCK_DAYS = {"SP01": 6, "SP02": 3, "SP03": 9}
INITIAL_RECEIPT_OFFSET = {"SP01": 2, "SP02": 1, "SP03": 3}
SAFETY_DAYS = {"I04": 0.3, "I06": 0.6}  # 기본 1.0. 번·양상추는 빠듯하게 → 가끔 품절
MISSED_ORDER = (date(2026, 9, 9), "S01", "I04")  # 발주 누락 시나리오 → 다음날 품절 → 긴급발주
DELAYED_LINE = ("SP02", "S02", "I05", date(2026, 9, 1))  # 이 조건의 첫 라인은 하루(다음 입고 가능일) 늦게
PARTIAL_LINE = ("SP03", "S01", "I14", date(2026, 8, 20))  # 이 조건의 첫 라인은 80%만 입고

PROMOTIONS = [
    dict(promo_id="P001", store_id="S01", start_date=date(2026, 8, 17), end_date=date(2026, 8, 23),
         promo_type="할인", target_menu="M02,M05", factor=1.6),
    dict(promo_id="P002", store_id="S03", start_date=date(2026, 9, 7), end_date=date(2026, 9, 13),
         promo_type="1+1", target_menu="M12", factor=1.8),
    dict(promo_id="P003", store_id="S02", start_date=date(2026, 9, 1), end_date=date(2026, 9, 6),
         promo_type="신메뉴", target_menu="M10", factor=1.4),
]
AVAILABILITY_EXCEPTIONS = {
    ("S03", "M10"): [(date(2026, 1, 1), date(2026, 8, 31), "미취급", "신메뉴 출시 전"),
                     (date(2026, 9, 1), date(2026, 9, 30), "취급", "9월 신메뉴 출시")],
    ("S02", "M04"): [(date(2026, 1, 1), date(2026, 9, 14), "취급", ""),
                     (date(2026, 9, 15), date(2026, 9, 30), "미취급", "새우패티 수급 문제로 판매 중단")],
}
TRANSFER_PLAN = [  # 날짜, 시, 출발, 도착, 식자재, 계획 수량, 사유
    (date(2026, 8, 12), 14, "S01", "S02", "I04", 48, "재고 부족 지원"),
    (date(2026, 9, 3), 15, "S03", "S01", "I06", 1000, "재고 부족 지원"),
    (date(2026, 9, 21), 16, "S02", "S03", "I01", 30, "매장 간 재고 조정"),
]
HOLIDAYS = {
    "2026-01-01": "신정", "2026-02-16": "설날 연휴", "2026-02-17": "설날", "2026-02-18": "설날 연휴",
    "2026-03-01": "삼일절", "2026-03-02": "대체공휴일(삼일절)", "2026-05-05": "어린이날",
    "2026-05-24": "부처님오신날", "2026-05-25": "대체공휴일(부처님오신날)", "2026-06-03": "전국동시지방선거",
    "2026-06-06": "현충일", "2026-08-15": "광복절", "2026-08-17": "대체공휴일(광복절)",
    "2026-09-24": "추석 연휴", "2026-09-25": "추석", "2026-09-26": "추석 연휴",
    "2026-10-03": "개천절", "2026-10-05": "대체공휴일(개천절)", "2026-10-09": "한글날",
}


# ---------------------------------------------------------------------------
# 도우미
# ---------------------------------------------------------------------------
@dataclass
class Lot:
    lot_id: str
    receipt_date: date
    last_use_date: date
    qty: int


def fefo_take(lots: list[Lot], amount: int) -> list[tuple[Lot, int]]:
    """사용기한 빠른 로트부터 amount 만큼 뺀다 (lots 를 제자리에서 줄임). (로트, 뺀 양) 목록 반환."""
    taken: list[tuple[Lot, int]] = []
    remaining = amount
    lots.sort(key=lambda lot: (lot.last_use_date, lot.receipt_date, lot.lot_id))
    for lot in lots:
        if remaining <= 0:
            break
        q = min(lot.qty, remaining)
        if q > 0:
            lot.qty -= q
            remaining -= q
            taken.append((lot, q))
    lots[:] = [lot for lot in lots if lot.qty > 0]
    if remaining != 0:
        raise RuntimeError("재고보다 많이 빼려 함 (fixture 버그)")
    return taken


def on_hand(lots: list[Lot]) -> int:
    return sum(lot.qty for lot in lots)


def price_asof(ing: str, d: date) -> int:
    price = ING[ing]["price"]
    for since, p in PRICE_CHANGES.get(ing, []):
        if d >= since:
            price = p
    return price


def arrival_date(sup: str, order_date: date) -> date:
    """발주일 + 리드타임 후, 입고 가능 요일까지 뒤로 민 날 (prep.SupplierCalendar 와 같은 규칙)."""
    d = order_date + timedelta(days=SUP[sup]["lead_time_days"])
    while d.isoweekday() not in SUP_DELIVERY_DAYS[sup]:
        d += timedelta(days=1)
    return d


def next_order_day(sup: str, after: date) -> date:
    d = after + timedelta(days=1)
    while d.isoweekday() not in SUP_ORDER_DAYS[sup]:
        d += timedelta(days=1)
    return d


def next_delivery_day(sup: str, after: date) -> date:
    d = after + timedelta(days=1)
    while d.isoweekday() not in SUP_DELIVERY_DAYS[sup]:
        d += timedelta(days=1)
    return d


def store_hours(store: dict[str, Any]) -> list[int]:
    return list(range(store["open_hour"], store["close_hour"]))


def hour_weights(hours: list[int]) -> dict[int, float]:
    def w(h: int) -> float:
        base = 0.4
        if h in (12, 13):
            base += 1.6
        elif h in (11, 14):
            base += 0.8
        if h in (18, 19):
            base += 1.2
        elif h in (17, 20):
            base += 0.6
        return base
    total = sum(w(h) for h in hours)
    return {h: w(h) / total for h in hours}


def availability_status(store: str, menu: str, d: date) -> bool:
    periods = AVAILABILITY_EXCEPTIONS.get((store, menu))
    if not periods:
        return True
    for start, end, status, _ in periods:
        if start <= d <= end:
            return status == "취급"
    return False


def expected_daily_usage(store: str, ing: str) -> float:
    size = next(s["relative_size"] for s in STORES if s["store_id"] == store)
    mean_wd = sum(WEEKDAY_FACTOR[store].values()) / 7.0
    return sum(MENU_BASE[m] * size * BOM[m].get(ing, 0) for m in MENU_IDS) * mean_wd


# ---------------------------------------------------------------------------
# 시뮬레이션
# ---------------------------------------------------------------------------
class FixtureSimulation:
    """하루 단위 시뮬레이션. run() 후 tables() 로 17개 DataFrame 을 얻는다."""

    def __init__(self, seed: int = 7, start: date = START, end: date = END) -> None:
        self.rng = np.random.default_rng(seed)
        self.start, self.end = start, end
        self.days = [start + timedelta(days=n) for n in range((end - start).days + 1)]
        self.lots: dict[tuple[str, str], list[Lot]] = {(s, i): [] for s in STORE_IDS for i in ING_IDS}
        self.usage = {(s, i): expected_daily_usage(s, i) for s in STORE_IDS for i in ING_IDS}
        self.pipeline: list[dict[str, Any]] = []
        self.rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self.seq = defaultdict(int)
        self.special_done = {"delayed": False, "partial": False}
        self.hour_w = {s["store_id"]: hour_weights(store_hours(s)) for s in STORES}

    # --- 식별자 -----------------------------------------------------------
    def _next_id(self, prefix: str, width: int) -> str:
        self.seq[prefix] += 1
        return f"{prefix}{self.seq[prefix]:0{width}d}"

    # --- 초기 재고 --------------------------------------------------------
    def _init_stock(self) -> None:
        for s in STORE_IDS:
            for i in ING_IDS:
                sup = ING[i]["supplier"]
                qty = int(round(self.usage[(s, i)] * INITIAL_STOCK_DAYS[sup]))
                if qty <= 0:
                    continue
                rd = self.start - timedelta(days=INITIAL_RECEIPT_OFFSET[sup])
                self.lots[(s, i)].append(Lot(f"LOT-INIT-{s}-{i}", rd, rd + timedelta(days=ING[i]["shelf"] - 1), qty))

    # --- 발주 ------------------------------------------------------------
    def _receipt_plan(self, sup: str, store: str, ing: str, order_date: date, expected: date,
                      order_qty: int, order_type: str) -> tuple[date, int, str]:
        """(실제 입고일, 입고 수량(발주단위), 상태) — 지연/부분입고 시나리오를 여기서 넣는다."""
        if order_type == "정기" and not self.special_done["delayed"]:
            dsup, dstore, ding, since = DELAYED_LINE
            if (sup, store, ing) == (dsup, dstore, ding) and order_date >= since:
                self.special_done["delayed"] = True
                return next_delivery_day(sup, expected), order_qty, "지연"
        if order_type == "정기" and not self.special_done["partial"]:
            psup, pstore, ping, since = PARTIAL_LINE
            if (sup, store, ing) == (psup, pstore, ping) and order_date >= since:
                self.special_done["partial"] = True
                return expected, max(1, int(math.floor(order_qty * 0.8))), "부분입고"
        return expected, order_qty, "정상"

    def _create_po(self, day: date, sup: str, store: str, lines: list[tuple[str, int]], order_type: str,
                   order_time: str, expected: date) -> None:
        po_id = self._next_id("PO", 5)
        for k, (ing, n) in enumerate(lines, start=1):
            base = price_asof(ing, day)
            unit_price = int(round(base * 1.1)) if order_type == "긴급" else base
            line_id = f"{po_id}-{k:02d}"
            self.rows["po"].append(dict(
                po_id=po_id, po_line_id=line_id, order_date=day, order_time=order_time, store_id=store,
                supplier_id=sup, ingredient_id=ing, order_qty=n, order_unit=ING[ing]["order_unit"],
                unit_price=unit_price, order_amount=n * unit_price, expected_date=expected, order_type=order_type))
            rdate, rqty, status = self._receipt_plan(sup, store, ing, day, expected, n, order_type)
            self.pipeline.append(dict(receipt_date=rdate, store=store, ing=ing, received_qty=rqty,
                                      use_qty=rqty * ING[ing]["conv"], po_id=po_id, po_line_id=line_id,
                                      expected=expected, status=status))

    def _regular_order(self, day: date, sup: str, store: str) -> None:
        arr1 = arrival_date(sup, day)
        nxt = next_order_day(sup, day)
        arr2 = arrival_date(sup, nxt)
        while arr2 <= arr1:
            nxt = next_order_day(sup, nxt)
            arr2 = arrival_date(sup, nxt)
        cover_days = (arr2 - day).days
        lines: list[list[Any]] = []
        for ing in [i for i in ING_IDS if ING[i]["supplier"] == sup]:
            if (day, store, ing) == MISSED_ORDER:
                continue  # 발주 누락 시나리오
            u = self.usage[(store, ing)]
            pipe = sum(p["use_qty"] for p in self.pipeline if p["store"] == store and p["ing"] == ing)
            need = u * (cover_days + SAFETY_DAYS.get(ing, 1.0)) - on_hand(self.lots[(store, ing)]) - pipe
            if need <= 0:
                continue
            n = max(int(math.ceil(need / ING[ing]["conv"])), ING[ing]["moq"])
            lines.append([ing, n])
        if not lines:
            return
        total = sum(n * price_asof(i, day) for i, n in lines)
        while total < SUP[sup]["min_order_amount"]:
            biggest = max(lines, key=lambda line: self.usage[(store, line[0])] * price_asof(line[0], day))
            biggest[1] += 1
            total += price_asof(biggest[0], day)
        minutes = SUP_CUTOFF_MIN[sup] - int(self.rng.integers(20, 150))
        order_time = f"{minutes // 60:02d}:{minutes % 60:02d}"
        self._create_po(day, sup, store, [(i, n) for i, n in lines], "정기", order_time, arr1)

    def _emergency_orders(self, day: date, stockouts: dict[tuple[str, str], dict[str, Any]],
                          daily: dict[tuple[str, str], dict[str, Any]]) -> None:
        by_store_sup: dict[tuple[str, str], list[tuple[str, int]]] = defaultdict(list)
        for (store, ing) in sorted(stockouts):
            n = max(int(math.ceil(self.usage[(store, ing)] * 2 / ING[ing]["conv"])), ING[ing]["moq"])
            by_store_sup[(store, ING[ing]["supplier"])].append((ing, n))
            daily[(store, ing)]["emergency"] = 1
        for (store, sup), lines in sorted(by_store_sup.items()):
            self._create_po(day, sup, store, lines, "긴급", "16:30", day + timedelta(days=1))

    # --- 하루 일과 --------------------------------------------------------
    def _morning(self, day: date, daily: dict, waste: dict) -> None:
        for s in STORE_IDS:
            for i in ING_IDS:
                lots, rec = self.lots[(s, i)], daily[(s, i)]
                rec["opening"] = on_hand(lots)
                if day.isoweekday() == 7 and rec["opening"] > 0 and self.rng.random() < 0.25:
                    adj = int(round(rec["opening"] * self.rng.uniform(-0.02, 0.01)))
                    if adj < 0:
                        fefo_take(lots, -adj)
                    elif adj > 0:
                        max(lots, key=lambda lot: (lot.last_use_date, lot.receipt_date)).qty += adj
                    rec["adjust"] = adj
                expired = [lot for lot in lots if lot.last_use_date < day]
                if expired:
                    q = on_hand(expired)
                    lots[:] = [lot for lot in lots if lot.last_use_date >= day]
                    rec["waste"] += q
                    waste[(s, i, "유통기한 경과")] += q

    def _receive(self, day: date, daily: dict) -> None:
        arriving = [p for p in self.pipeline if p["receipt_date"] == day]
        self.pipeline = [p for p in self.pipeline if p["receipt_date"] != day]
        for p in arriving:
            rid = self._next_id("RC", 5)
            ing = p["ing"]
            self.lots[(p["store"], ing)].append(
                Lot(f"LOT-{rid}", day, day + timedelta(days=ING[ing]["shelf"] - 1), p["use_qty"]))
            daily[(p["store"], ing)]["received"] += p["use_qty"]
            self.rows["receipt"].append(dict(
                receipt_id=rid, po_line_id=p["po_line_id"], po_id=p["po_id"], store_id=p["store"],
                ingredient_id=ing, receipt_date=day, received_qty=p["received_qty"],
                order_unit=ING[ing]["order_unit"], received_use_qty=p["use_qty"], status=p["status"],
                delay_days=(day - p["expected"]).days))

    def _transfer(self, day: date, plan: tuple, daily: dict) -> None:
        _, hour, src, dst, ing, planned, reason = plan
        q = min(planned, on_hand(self.lots[(src, ing)]))
        if q <= 0:
            return
        tid = self._next_id("TR", 4)
        for lot, amount in fefo_take(self.lots[(src, ing)], q):
            self.lots[(dst, ing)].append(Lot(f"{lot.lot_id}-{tid}", lot.receipt_date, lot.last_use_date, amount))
        daily[(src, ing)]["tout"] += q
        daily[(dst, ing)]["tin"] += q
        self.rows["transfer"].append(dict(transfer_id=tid, date=day, hour=hour, from_store_id=src, to_store_id=dst,
                                          ingredient_id=ing, qty=q, use_unit=ING[ing]["use_unit"], reason=reason))

    def _promo(self, store: str, day: date) -> tuple[int, dict[str, float]]:
        flag, factors = 0, {}
        for p in PROMOTIONS:
            if p["store_id"] == store and p["start_date"] <= day <= p["end_date"]:
                flag = 1
                for m in p["target_menu"].split(","):
                    factors[m] = max(factors.get(m, 1.0), p["factor"])
        return flag, factors

    def _sell_hour(self, day: date, hour: int, store: str, daily: dict, stockouts: dict) -> None:
        is_hol = day.isoformat() in HOLIDAYS
        wd = WEEKDAY_FACTOR[store][day.isoweekday()] * (HOLIDAY_FACTOR[store] if is_hol else 1.0)
        size = next(s["relative_size"] for s in STORES if s["store_id"] == store)
        promo_flag, promo_factor = self._promo(store, day)
        menus = [m for m in MENU_IDS if availability_status(store, m, day)]
        for idx in self.rng.permutation(len(menus)):
            m = menus[int(idx)]
            for ch, share in CHANNEL_SHARE[store].items():
                lam = MENU_BASE[m] * size * self.hour_w[store][hour] * share * wd * promo_factor.get(m, 1.0)
                demand = int(self.rng.poisson(lam))
                if demand == 0:
                    continue
                caps = {i: on_hand(self.lots[(store, i)]) // q for i, q in BOM[m].items()}
                qty = min(demand, min(caps.values()))
                lost = demand - qty
                cancel = int(self.rng.binomial(qty, CANCEL_P[ch])) if qty > 0 else 0
                net = qty - cancel
                for i, q in BOM[m].items():
                    if net * q > 0:
                        fefo_take(self.lots[(store, i)], net * q)
                        daily[(store, i)]["consumed"] += net * q
                if lost > 0:
                    for i, cap in caps.items():
                        if cap < demand:
                            so = stockouts.setdefault((store, i), {"hour": hour, "menus": []})
                            if m not in so["menus"]:
                                so["menus"].append(m)
                self.rows["sales"].append(dict(
                    date=day, hour=hour, store_id=store, menu_id=m, channel=ch, demand_qty=demand, qty=qty,
                    cancel_qty=cancel, lost_qty=lost, sales_amount=net * MENU_PRICE[m], promo_flag=promo_flag))

    def _evening(self, day: date, daily: dict, waste: dict) -> None:
        for s in STORE_IDS:
            for i in ING_IDS:
                lots = self.lots[(s, i)]
                if on_hand(lots) > 0 and self.rng.random() < 0.01:
                    q = min(on_hand(lots), max(1, int(round(self.usage[(s, i)] * 0.03))))
                    fefo_take(lots, q)
                    daily[(s, i)]["waste"] += q
                    waste[(s, i, "품질 불량")] += q

    def _record(self, day: date, daily: dict, waste: dict, stockouts: dict) -> None:
        for s in STORE_IDS:
            for i in ING_IDS:
                rec, lots = daily[(s, i)], self.lots[(s, i)]
                closing = on_hand(lots)
                expect = (rec["opening"] + rec["received"] + rec["adjust"] + rec["tin"]
                          - rec["consumed"] - rec["waste"] - rec["tout"])
                if closing != expect:
                    raise RuntimeError(f"재고 항등식 불일치 {day} {s} {i}: {closing} != {expect}")
                conv, price = ING[i]["conv"], price_asof(i, day)
                self.rows["inventory"].append(dict(
                    date=day, store_id=s, ingredient_id=i, use_unit=ING[i]["use_unit"], opening_qty=rec["opening"],
                    received_qty=rec["received"], adjust_qty=rec["adjust"], counted_qty=rec["opening"] + rec["adjust"],
                    consumed_qty=rec["consumed"], waste_qty=rec["waste"], transfer_in_qty=rec["tin"],
                    transfer_out_qty=rec["tout"], closing_qty=closing, closing_value=int(round(closing / conv * price)),
                    stockout_flag=int((s, i) in stockouts), emergency_flag=rec["emergency"]))
                for lot in sorted(lots, key=lambda x: (x.last_use_date, x.lot_id)):
                    self.rows["lots"].append(dict(
                        date=day, store_id=s, ingredient_id=i, lot_id=lot.lot_id, receipt_date=lot.receipt_date,
                        last_use_date=lot.last_use_date, remaining_qty=lot.qty, use_unit=ING[i]["use_unit"]))
        for (s, i, reason), q in sorted(waste.items()):
            if q > 0:
                self.rows["waste"].append(dict(
                    date=day, store_id=s, ingredient_id=i, waste_qty=q, use_unit=ING[i]["use_unit"], reason=reason,
                    waste_cost=int(round(q / ING[i]["conv"] * price_asof(i, day)))))
        for (s, i), so in sorted(stockouts.items()):
            report = "POS 자동" if self.rng.random() < 0.7 else "점장 보고"
            self.rows["stockout"].append(dict(date=day, store_id=s, ingredient_id=i, first_lost_hour=so["hour"],
                                              affected_menus=",".join(sorted(so["menus"])), report=report))

    def run(self) -> "FixtureSimulation":
        self._init_stock()
        for day in self.days:
            daily = {(s, i): dict(opening=0, received=0, adjust=0, consumed=0, waste=0, tin=0, tout=0, emergency=0)
                     for s in STORE_IDS for i in ING_IDS}
            waste: dict[tuple[str, str, str], int] = defaultdict(int)
            stockouts: dict[tuple[str, str], dict[str, Any]] = {}
            self._morning(day, daily, waste)
            self._receive(day, daily)
            for sup in SUP:
                if day.isoweekday() in SUP_ORDER_DAYS[sup]:
                    for s in STORE_IDS:
                        self._regular_order(day, sup, s)
            for hour in range(24):
                for plan in TRANSFER_PLAN:
                    if plan[0] == day and plan[1] == hour:
                        self._transfer(day, plan, daily)
                for store in STORES:
                    if hour in self.hour_w[store["store_id"]]:
                        self._sell_hour(day, hour, store["store_id"], daily, stockouts)
            self._evening(day, daily, waste)
            self._emergency_orders(day, stockouts, daily)
            self._record(day, daily, waste, stockouts)
        return self

    # --- 출력 ------------------------------------------------------------
    def tables(self) -> dict[str, pd.DataFrame]:
        t: dict[str, pd.DataFrame] = {}
        t["01_stores"] = pd.DataFrame(STORES)
        t["02_menus"] = pd.DataFrame([dict(menu_id=m[0], menu_name=m[1], category=m[2], is_set=m[3],
                                           set_components=m[4], virtual_price=m[5]) for m in MENUS])
        t["03_ingredients"] = pd.DataFrame([dict(
            ingredient_id=i, ingredient_name=v["name"], supplier_id=v["supplier"], use_unit=v["use_unit"],
            order_unit=v["order_unit"], use_qty_per_order_unit=v["conv"], storage=v["storage"],
            shelf_life_days=v["shelf"], min_order_qty=v["moq"]) for i, v in ING.items()])
        t["04_bom"] = pd.DataFrame([dict(menu_id=m, ingredient_id=i, qty_per_menu=q, use_unit=ING[i]["use_unit"])
                                    for m, items in BOM.items() for i, q in items.items()])
        t["05_suppliers"] = pd.DataFrame(SUPPLIERS)
        prices = [dict(ingredient_id=i, valid_from=CAL_START, unit_price=v["price"]) for i, v in ING.items()]
        prices += [dict(ingredient_id=i, valid_from=d, unit_price=p) for i, ch in PRICE_CHANGES.items() for d, p in ch]
        t["06_ingredient_price_history"] = pd.DataFrame(prices).sort_values(["ingredient_id", "valid_from"])
        cal_days = [CAL_START + timedelta(days=n) for n in range((CAL_END - CAL_START).days + 1)]
        t["07_calendar"] = pd.DataFrame([dict(date=d, weekday=KOR_DAYS[d.isoweekday() - 1],
                                              is_holiday=int(d.isoformat() in HOLIDAYS),
                                              holiday_name=HOLIDAYS.get(d.isoformat(), "")) for d in cal_days])
        t["08_promotions"] = pd.DataFrame([{k: v for k, v in p.items() if k != "factor"} for p in PROMOTIONS])
        t["09_sales"] = pd.DataFrame(self.rows["sales"]).sort_values(["date", "hour", "store_id", "menu_id", "channel"])
        t["10_inventory_daily"] = self._inventory_frame()
        t["11_purchase_orders"] = pd.DataFrame(self.rows["po"])
        t["12_receipts"] = pd.DataFrame(self.rows["receipt"])
        t["13_waste"] = pd.DataFrame(self.rows["waste"])
        t["14_transfers"] = pd.DataFrame(self.rows["transfer"])
        avail = []
        for s in STORE_IDS:
            for m in MENU_IDS:
                periods = AVAILABILITY_EXCEPTIONS.get((s, m), [(CAL_START, self.end, "취급", "")])
                for start, end, status, note in periods:
                    avail.append(dict(store_id=s, menu_id=m, start_date=start, end_date=end, status=status, note=note))
        t["15_menu_availability"] = pd.DataFrame(avail)
        t["16_inventory_lots_daily"] = pd.DataFrame(self.rows["lots"])
        t["17_stockouts"] = pd.DataFrame(self.rows["stockout"],
                                         columns=["date", "store_id", "ingredient_id", "first_lost_hour",
                                                  "affected_menus", "report"])
        return t

    def _inventory_frame(self) -> pd.DataFrame:
        inv = pd.DataFrame(self.rows["inventory"]).sort_values(["store_id", "ingredient_id", "date"])
        inv["avg_consumption_7d"] = (inv.groupby(["store_id", "ingredient_id"])["consumed_qty"]
                                     .transform(lambda s: s.rolling(7, min_periods=1).mean()).round(3))
        avg = inv["avg_consumption_7d"]
        cover = np.where(avg > 0, inv["closing_qty"] / avg.where(avg > 0, 1.0), np.inf)
        inv["low_stock_flag"] = ((avg > 0) & (cover < 1.5)).astype(int)
        inv["overstock_flag"] = (((avg > 0) & (cover > 7.0)) | ((avg == 0) & (inv["closing_qty"] > 0))).astype(int)
        cols = ["date", "store_id", "ingredient_id", "use_unit", "opening_qty", "received_qty", "adjust_qty",
                "counted_qty", "consumed_qty", "waste_qty", "transfer_in_qty", "transfer_out_qty", "closing_qty",
                "closing_value", "stockout_flag", "emergency_flag", "avg_consumption_7d", "low_stock_flag",
                "overstock_flag"]
        return inv.sort_values(["date", "store_id", "ingredient_id"])[cols]


def write_tables(tables: dict[str, pd.DataFrame], out_dir: Path) -> None:
    """명세 형식으로 저장: UTF-8, 헤더 1줄, 날짜 YYYY-MM-DD, 인덱스 없음."""
    out_dir.mkdir(parents=True, exist_ok=True)
    for key, df in tables.items():
        df.to_csv(out_dir / f"{key}.csv", index=False, encoding="utf-8", date_format="%Y-%m-%d")


def generate(out_dir: Path = DEFAULT_OUT, seed: int = 7, start: date = START) -> dict[str, int]:
    """fixture 를 만들어 out_dir 에 쓰고, 테이블별 행 수를 돌려준다. start 를 2026-01-01 로 주면 전체 기간."""
    sim = FixtureSimulation(seed=seed, start=start).run()
    tables = sim.tables()
    write_tables(tables, Path(out_dir))
    return {k: int(len(v)) for k, v in tables.items()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="명세 형식 합성 fixture 생성")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="출력 폴더 (기본 data/fixture)")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--start", default=START.isoformat(),
                        help="시작일 (기본 2026-08-02 = 60일). 2026-01-01 이면 명세 전체 기간 (약 4.5배 크기)")
    args = parser.parse_args(argv)
    counts = generate(Path(args.out), args.seed, date.fromisoformat(args.start))
    for key, n in counts.items():
        print(f"{key:30s} {n:>8,d} rows")
    print(f"저장 위치: {Path(args.out).resolve()}")
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass
    raise SystemExit(main())
