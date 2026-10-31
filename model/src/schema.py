"""
데이터 명세서(docs/data_spec.md)를 코드로 옮긴 선언적 스키마.

이 파일만 보면 17개 CSV의 파일 키, 기대 파일명, 한 행의 기준(고유키), 컬럼별 종류(kind)와
명세서의 단위 설명, 그리고 테이블 간 연결(외래키)을 알 수 있다.
실제 데이터의 컬럼명이 명세와 다르면 loader가 load_report에 누락/추가 컬럼을 기록하므로,
여기의 컬럼 정의를 고치거나 loader의 이름 보정 결과를 확인하면 된다.

컬럼 kind 목록과 로더가 만드는 결과
- id           : 연결용 식별자. 문자열로 읽고 앞뒤 공백 제거. 모든 값이 '12.0' 형태면 '12'로 정리.
- str          : 자유 문자열(이름, 메모). 앞뒤 공백만 제거.
- category     : 분류값(채널, 상태 코드, 사유 등). 문자열 유지. 프로파일에 값 목록을 출력.
- date         : YYYY-MM-DD 날짜 → datetime64[ns] (시각 없음). 해석 실패는 NaT + 경고.
- time_hhmm    : HH:MM 시각. 원본 문자열은 <col> 그대로 두고, <col>_min (자정 기준 분, 정수) 추가.
- hour         : 시간대(시). '10', '10:00', '10시' → 10 (정수). 10은 10시대.
- int          : 정수. 결측이 없으면 int64, 결측이 있으면 float64. 소수가 섞이면 float64로 두고 경고.
- float        : 실수 → float64. 천 단위 쉼표는 제거 후 변환.
- flag01       : 0/1 플래그 → 0/1 정수(int64, 결측 있으면 float64).
- yn           : Y/N 여부 → bool (결측 있으면 pandas 'boolean').
- list         : ID 목록 → 파이썬 list[str]. 원본 문자열은 <col>_raw.
- weekday_list : 요일 목록 → 파이썬 list[int] (ISO: 1=월 … 7=일). 원본 문자열은 <col>_raw.
- weekday      : 단일 요일(달력). 원본은 <col> 그대로, <col>_iso (1=월 … 7=일) 추가.

이름 규칙: list/weekday_list 는 파싱 결과를 제자리(<col>)에, 원본은 <col>_raw 에 둔다.
time_hhmm/weekday 는 원본을 <col> 에 두고 파생값을 <col>_min / <col>_iso 에 둔다.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import pandas as pd

# ---------------------------------------------------------------------------
# 기간 상수 (명세서: 기록 기간 2026-01-01 ~ 2026-09-30)
# ---------------------------------------------------------------------------
DATA_START = pd.Timestamp("2026-01-01")
DATA_END = pd.Timestamp("2026-09-30")
FORECAST_START = pd.Timestamp("2026-10-01")  # 예측 구간 시작 (기록 기간 다음 날)

KINDS = (
    "id", "str", "category", "date", "time_hhmm", "hour", "int", "float",
    "flag01", "yn", "list", "weekday_list", "weekday",
)
NUMERIC_KINDS = ("int", "float", "flag01", "hour")


@dataclass(frozen=True)
class ColumnSpec:
    """컬럼 하나의 정의. unit 은 명세서의 '단위·형식' 칸, desc 는 '뜻' 칸."""

    name: str
    kind: str
    unit: str = ""
    desc: str = ""

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ValueError(f"알 수 없는 kind: {self.kind} ({self.name})")


@dataclass(frozen=True)
class TableSpec:
    """CSV 파일 하나의 정의.

    key         : 파일 키 (예: '09_sales'). 앞 두 자리 숫자로 파일을 찾는다.
    primary_key : 한 행의 기준이 되는 컬럼들 (고유키). 중복 검사에 쓴다.
    soft_key    : True 이면 명세에 식별자가 없어 '추정 키'로만 검사한다 (중복이어도 WARN).
    name_hints  : 파일명이 다를 때 이름 유사도 매칭에 쓰는 단어들.
    """

    key: str
    title: str
    grain_desc: str
    primary_key: tuple[str, ...]
    columns: tuple[ColumnSpec, ...]
    name_hints: tuple[str, ...] = ()
    soft_key: bool = False

    @property
    def number(self) -> int:
        return int(self.key[:2])

    @property
    def filename(self) -> str:
        return f"{self.key}.csv"

    @property
    def short_name(self) -> str:
        return self.key[3:]

    @property
    def column_names(self) -> list[str]:
        return [c.name for c in self.columns]

    def column(self, name: str) -> Optional[ColumnSpec]:
        for c in self.columns:
            if c.name == name:
                return c
        return None

    def columns_of_kind(self, *kinds: str) -> list[str]:
        return [c.name for c in self.columns if c.kind in kinds]


def C(name: str, kind: str, unit: str = "", desc: str = "") -> ColumnSpec:
    """ColumnSpec 짧은 생성자."""
    return ColumnSpec(name=name, kind=kind, unit=unit, desc=desc)


# ---------------------------------------------------------------------------
# 17개 테이블 정의 (명세서 순서 그대로)
# ---------------------------------------------------------------------------
TABLES: tuple[TableSpec, ...] = (
    TableSpec(
        key="01_stores", title="매장 기본 정보", grain_desc="매장",
        primary_key=("store_id",), name_hints=("stores", "store", "매장"),
        columns=(
            C("store_id", "id", "ID", "매장 식별자"),
            C("store_name", "str", "문자", "매장명"),
            C("trade_area", "category", "분류", "상권 유형"),
            C("store_type", "category", "분류", "매장 형태"),
            C("open_hour", "hour", "시", "영업 시작 시간대"),
            C("close_hour", "hour", "시", "판매 시간대 기준 영업 종료 시각"),
            C("dine_in", "yn", "Y=운영, N=미운영", "매장 식사 운영 여부"),
            C("delivery", "yn", "Y=운영, N=미운영", "배달 운영 여부"),
            C("relative_size", "float", "배율, S01=1", "S01 매장 대비 상대 판매 규모"),
        ),
    ),
    TableSpec(
        key="02_menus", title="메뉴 기본 정보", grain_desc="메뉴",
        primary_key=("menu_id",), name_hints=("menus", "menu", "메뉴"),
        columns=(
            C("menu_id", "id", "ID", "메뉴 식별자"),
            C("menu_name", "str", "문자", "메뉴명"),
            C("category", "category", "분류", "항목 분류"),
            C("is_set", "flag01", "1=세트, 0=단품", "세트 메뉴 여부"),
            C("set_components", "list", "ID 목록", "세트를 구성하는 메뉴 식별자 목록 (세트 BOM 계산에 전개하지 않음)"),
            C("virtual_price", "float", "원/메뉴 1개", "가상 메뉴 판매가"),
        ),
    ),
    TableSpec(
        key="03_ingredients", title="식자재 기본 정보", grain_desc="식자재",
        primary_key=("ingredient_id",), name_hints=("ingredients", "ingredient", "식자재"),
        columns=(
            C("ingredient_id", "id", "ID", "식자재 식별자"),
            C("ingredient_name", "str", "문자", "식자재명"),
            C("supplier_id", "id", "ID", "공급사 식별자"),
            C("use_unit", "category", "개·g·ml·매·캔·장 등", "식자재 사용량·재고량의 기준 단위"),
            C("order_unit", "category", "박스·팩·트레이·EA·BOX 등", "발주·입고 수량의 기준 포장 단위"),
            C("use_qty_per_order_unit", "float", "사용단위/발주단위", "발주단위 1개에 포함된 사용단위 수량"),
            C("storage", "category", "냉동·냉장·상온", "보관 방식"),
            C("shelf_life_days", "int", "일 (입고일 포함)", "입고일을 포함한 사용 가능 기간 → last_use = 입고일 + shelf − 1"),
            C("min_order_qty", "int", "발주단위", "품목별 최소 발주 수량"),
        ),
    ),
    TableSpec(
        key="04_bom", title="메뉴별 식자재 소요량 (BOM)", grain_desc="메뉴 × 식자재",
        primary_key=("menu_id", "ingredient_id"), name_hints=("bom", "recipe", "레시피", "소요량"),
        columns=(
            C("menu_id", "id", "ID", "메뉴 식별자"),
            C("ingredient_id", "id", "ID", "식자재 식별자"),
            C("qty_per_menu", "float", "use_unit/메뉴 1개", "메뉴 1개에 필요한 해당 식자재 사용량"),
            C("use_unit", "category", "개·g·ml·매·캔·장 등", "식자재 사용량·재고량의 기준 단위"),
        ),
    ),
    TableSpec(
        key="05_suppliers", title="공급사 발주 조건", grain_desc="공급사",
        primary_key=("supplier_id",), name_hints=("suppliers", "supplier", "vendor", "공급사"),
        columns=(
            C("supplier_id", "id", "ID", "공급사 식별자"),
            C("supplier_name", "str", "문자", "공급사명"),
            C("order_days", "weekday_list", "요일 목록", "발주 가능한 요일"),
            C("order_cutoff", "time_hhmm", "HH:MM", "발주 마감 시각"),
            C("lead_time_days", "int", "일", "발주 후 입고까지의 기간"),
            C("delivery_days", "weekday_list", "요일 목록", "입고 가능한 요일"),
            C("min_order_amount", "float", "원, 0=제한 없음", "발주서 1건의 최소 발주금액"),
            C("note", "str", "문자", "보충 설명 또는 메모"),
        ),
    ),
    TableSpec(
        key="06_ingredient_price_history", title="식자재 단가 이력", grain_desc="식자재 × 적용 시작일",
        primary_key=("ingredient_id", "valid_from"),
        name_hints=("ingredient_price_history", "price_history", "price", "단가"),
        columns=(
            C("ingredient_id", "id", "ID", "식자재 식별자"),
            C("valid_from", "date", "YYYY-MM-DD", "단가 적용 시작일"),
            C("unit_price", "float", "원/발주단위", "발주단위 1개당 가상 단가"),
        ),
    ),
    TableSpec(
        key="07_calendar", title="달력", grain_desc="날짜",
        primary_key=("date",), name_hints=("calendar", "dates", "달력"),
        columns=(
            C("date", "date", "YYYY-MM-DD", "해당 기록의 기준일"),
            C("weekday", "weekday", "월~일", "요일"),
            C("is_holiday", "flag01", "1=공휴일, 0=아님", "공휴일 여부"),
            C("holiday_name", "category", "문자", "공휴일 이름"),
        ),
    ),
    TableSpec(
        key="08_promotions", title="프로모션", grain_desc="행사",
        primary_key=("promo_id",), name_hints=("promotions", "promotion", "promo", "프로모션"),
        columns=(
            C("promo_id", "id", "ID", "프로모션 식별자"),
            C("store_id", "id", "ID", "매장 식별자"),
            C("start_date", "date", "YYYY-MM-DD", "기간 시작일"),
            C("end_date", "date", "YYYY-MM-DD", "기간 종료일"),
            C("promo_type", "category", "문자", "프로모션 유형"),
            C("target_menu", "list", "ID 목록", "행사 대상 메뉴 식별자 목록"),
        ),
    ),
    TableSpec(
        key="09_sales", title="시간대별 판매", grain_desc="날짜 × 시간대 × 매장 × 메뉴 × 채널",
        primary_key=("date", "hour", "store_id", "menu_id", "channel"), name_hints=("sales", "판매"),
        columns=(
            C("date", "date", "YYYY-MM-DD", "해당 기록의 기준일"),
            C("hour", "hour", "시, 10은 10시대", "해당 기록의 시간대"),
            C("store_id", "id", "ID", "매장 식별자"),
            C("menu_id", "id", "ID", "메뉴 식별자"),
            C("channel", "category", "매장·포장·배달", "주문 채널"),
            C("demand_qty", "int", "메뉴 개수", "품절로 인한 판매 제한 전 수요량"),
            C("qty", "int", "메뉴 개수", "실제 주문 수량. 취소 수량을 차감하기 전 값"),
            C("cancel_qty", "int", "메뉴 개수", "취소·환불 수량"),
            C("lost_qty", "int", "메뉴 개수", "품절로 판매하지 못한 수량"),
            C("sales_amount", "float", "원", "취소 수량을 반영한 가상 판매금액"),
            C("promo_flag", "flag01", "0/1", "해당 매장·기간의 프로모션 여부 (대상 메뉴는 08.target_menu)"),
        ),
    ),
    TableSpec(
        key="10_inventory_daily", title="일별 재고", grain_desc="날짜 × 매장 × 식자재",
        primary_key=("date", "store_id", "ingredient_id"), name_hints=("inventory_daily", "inventory", "재고"),
        columns=(
            C("date", "date", "YYYY-MM-DD", "해당 기록의 기준일"),
            C("store_id", "id", "ID", "매장 식별자"),
            C("ingredient_id", "id", "ID", "식자재 식별자"),
            C("use_unit", "category", "개·g·ml·매·캔·장 등", "식자재 사용량·재고량의 기준 단위"),
            C("opening_qty", "float", "use_unit", "당일 입고 전 기초재고"),
            C("received_qty", "float", "use_unit", "해당 일자의 입고량"),
            C("adjust_qty", "float", "use_unit, 양수·음수", "재고 실사에 따른 증감 조정량"),
            C("counted_qty", "float", "use_unit", "영업 전 실사재고"),
            C("consumed_qty", "float", "use_unit", "판매에 사용한 식자재 소진량"),
            C("waste_qty", "float", "use_unit", "폐기한 식자재 수량"),
            C("transfer_in_qty", "float", "use_unit", "다른 매장에서 이동해 온 수량"),
            C("transfer_out_qty", "float", "use_unit", "다른 매장으로 보낸 수량"),
            C("closing_qty", "float", "use_unit", "영업 종료 후 기말재고"),
            C("closing_value", "float", "원", "기말재고의 가상 평가금액"),
            C("stockout_flag", "flag01", "0/1", "해당 식자재 부족으로 판매가 막혔는지 여부"),
            C("emergency_flag", "flag01", "0/1", "당일 긴급발주 발생 여부"),
            C("avg_consumption_7d", "float", "use_unit/일", "당일을 포함한 최근 7일 평균 소진량"),
            C("low_stock_flag", "flag01", "0/1", "제공 데이터의 저재고 표시"),
            C("overstock_flag", "flag01", "0/1", "제공 데이터의 과잉재고 표시"),
        ),
    ),
    TableSpec(
        key="11_purchase_orders", title="발주 내역", grain_desc="발주 라인",
        primary_key=("po_line_id",), name_hints=("purchase_orders", "purchase_order", "orders", "po", "발주"),
        columns=(
            C("po_id", "id", "ID", "발주서 식별자"),
            C("po_line_id", "id", "ID", "발주서 내 품목별 라인 식별자"),
            C("order_date", "date", "YYYY-MM-DD", "발주일"),
            C("order_time", "time_hhmm", "HH:MM", "발주 시각"),
            C("store_id", "id", "ID", "매장 식별자"),
            C("supplier_id", "id", "ID", "공급사 식별자"),
            C("ingredient_id", "id", "ID", "식자재 식별자"),
            C("order_qty", "float", "order_unit", "발주 수량"),
            C("order_unit", "category", "박스·팩·트레이·EA·BOX 등", "발주·입고 수량의 기준 포장 단위"),
            C("unit_price", "float", "원/발주단위", "발주단위 1개당 가상 단가"),
            C("order_amount", "float", "원", "발주 라인의 가상 금액"),
            C("expected_date", "date", "YYYY-MM-DD", "입고 예정일"),
            C("order_type", "category", "정기·긴급", "발주 유형"),
        ),
    ),
    TableSpec(
        key="12_receipts", title="입고 내역", grain_desc="입고 건",
        primary_key=("receipt_id",), name_hints=("receipts", "receipt", "입고"),
        columns=(
            C("receipt_id", "id", "ID", "입고 건 식별자"),
            C("po_line_id", "id", "ID", "발주서 내 품목별 라인 식별자"),
            C("po_id", "id", "ID", "발주서 식별자"),
            C("store_id", "id", "ID", "매장 식별자"),
            C("ingredient_id", "id", "ID", "식자재 식별자"),
            C("receipt_date", "date", "YYYY-MM-DD", "실제 입고일"),
            C("received_qty", "float", "order_unit", "발주단위 기준 실제 입고 수량"),
            C("order_unit", "category", "박스·팩·트레이·EA·BOX 등", "발주·입고 수량의 기준 포장 단위"),
            C("received_use_qty", "float", "use_unit", "입고량을 사용단위로 환산한 수량"),
            C("status", "category", "상태 코드", "입고 상태"),
            C("delay_days", "int", "일", "예정일 대비 입고 지연 기간"),
        ),
    ),
    TableSpec(
        key="13_waste", title="식자재 폐기",
        grain_desc="식자재 폐기 건 (식별자 없음 → 날짜×매장×식자재×사유를 추정 키로 사용)",
        primary_key=("date", "store_id", "ingredient_id", "reason"), soft_key=True,
        name_hints=("waste", "disposal", "폐기"),
        columns=(
            C("date", "date", "YYYY-MM-DD", "해당 기록의 기준일"),
            C("store_id", "id", "ID", "매장 식별자"),
            C("ingredient_id", "id", "ID", "식자재 식별자"),
            C("waste_qty", "float", "use_unit", "폐기한 식자재 수량"),
            C("use_unit", "category", "개·g·ml·매·캔·장 등", "식자재 사용량·재고량의 기준 단위"),
            C("reason", "category", "문자", "식자재 폐기 사유"),
            C("waste_cost", "float", "원", "폐기 식자재의 가상 비용"),
        ),
    ),
    TableSpec(
        key="14_transfers", title="매장 간 이동", grain_desc="이동 건",
        primary_key=("transfer_id",), name_hints=("transfers", "transfer", "이동"),
        columns=(
            C("transfer_id", "id", "ID", "매장 간 이동 건 식별자"),
            C("date", "date", "YYYY-MM-DD", "해당 기록의 기준일"),
            C("hour", "hour", "시, 10은 10시대", "해당 기록의 시간대"),
            C("from_store_id", "id", "매장 ID", "이동 출발 매장"),
            C("to_store_id", "id", "매장 ID", "이동 도착 매장"),
            C("ingredient_id", "id", "ID", "식자재 식별자"),
            C("qty", "float", "use_unit", "매장 간 이동한 식자재 수량"),
            C("use_unit", "category", "개·g·ml·매·캔·장 등", "식자재 사용량·재고량의 기준 단위"),
            C("reason", "category", "문자", "매장 간 이동 사유"),
        ),
    ),
    TableSpec(
        key="15_menu_availability", title="메뉴 취급 기간", grain_desc="매장 × 메뉴 × 기간",
        primary_key=("store_id", "menu_id", "start_date"),
        name_hints=("menu_availability", "availability", "취급"),
        columns=(
            C("store_id", "id", "ID", "매장 식별자"),
            C("menu_id", "id", "ID", "메뉴 식별자"),
            C("start_date", "date", "YYYY-MM-DD", "취급 상태 적용 시작일"),
            C("end_date", "date", "YYYY-MM-DD", "취급 상태 적용 종료일"),
            C("status", "category", "취급·미취급", "메뉴 취급 여부"),
            C("note", "str", "문자", "보충 설명 또는 메모"),
        ),
    ),
    TableSpec(
        key="16_inventory_lots_daily", title="입고 묶음별 재고",
        grain_desc="날짜 × 매장 × 식자재 × 입고 묶음 (영업 종료 후 잔량)",
        primary_key=("date", "store_id", "ingredient_id", "lot_id"),
        name_hints=("inventory_lots_daily", "inventory_lots", "lots", "lot", "로트", "입고묶음"),
        columns=(
            C("date", "date", "YYYY-MM-DD", "해당 기록의 기준일"),
            C("store_id", "id", "ID", "매장 식별자"),
            C("ingredient_id", "id", "ID", "식자재 식별자"),
            C("lot_id", "id", "ID", "입고 묶음 식별자"),
            C("receipt_date", "date", "YYYY-MM-DD", "실제 입고일"),
            C("last_use_date", "date", "YYYY-MM-DD", "마지막으로 사용할 수 있는 날짜"),
            C("remaining_qty", "float", "use_unit", "해당 일자 영업 종료 후 묶음별 잔량"),
            C("use_unit", "category", "개·g·ml·매·캔·장 등", "식자재 사용량·재고량의 기준 단위"),
        ),
    ),
    TableSpec(
        key="17_stockouts", title="품절 기록", grain_desc="날짜 × 매장 × 식자재",
        primary_key=("date", "store_id", "ingredient_id"), name_hints=("stockouts", "stockout", "품절"),
        columns=(
            C("date", "date", "YYYY-MM-DD", "해당 기록의 기준일"),
            C("store_id", "id", "ID", "매장 식별자"),
            C("ingredient_id", "id", "ID", "식자재 식별자"),
            C("first_lost_hour", "hour", "시", "품절로 처음 판매가 막힌 시간대"),
            C("affected_menus", "list", "메뉴 ID 목록", "품절로 판매 손실이 생긴 메뉴 목록"),
            C("report", "category", "문자", "품절 보고 방식"),
        ),
    ),
)

TABLE_BY_KEY: dict[str, TableSpec] = {t.key: t for t in TABLES}
TABLE_KEYS: tuple[str, ...] = tuple(t.key for t in TABLES)


# ---------------------------------------------------------------------------
# 외래키 (child.col -> parent.col)
# kind: 'id' 일반 ID, 'list' 목록 컬럼의 각 원소, 'date' 달력 날짜 연결
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ForeignKey:
    child: str
    child_col: str
    parent: str
    parent_col: str
    kind: str = "id"

    @property
    def label(self) -> str:
        return f"{self.child}.{self.child_col} → {self.parent}.{self.parent_col}"


def _fk(child: str, col: str, parent: str, pcol: str, kind: str = "id") -> ForeignKey:
    return ForeignKey(child, col, parent, pcol, kind)


FOREIGN_KEYS: tuple[ForeignKey, ...] = (
    _fk("02_menus", "set_components", "02_menus", "menu_id", "list"),
    _fk("03_ingredients", "supplier_id", "05_suppliers", "supplier_id"),
    _fk("04_bom", "menu_id", "02_menus", "menu_id"),
    _fk("04_bom", "ingredient_id", "03_ingredients", "ingredient_id"),
    _fk("06_ingredient_price_history", "ingredient_id", "03_ingredients", "ingredient_id"),
    _fk("08_promotions", "store_id", "01_stores", "store_id"),
    _fk("08_promotions", "target_menu", "02_menus", "menu_id", "list"),
    _fk("09_sales", "store_id", "01_stores", "store_id"),
    _fk("09_sales", "menu_id", "02_menus", "menu_id"),
    _fk("09_sales", "date", "07_calendar", "date", "date"),
    _fk("10_inventory_daily", "store_id", "01_stores", "store_id"),
    _fk("10_inventory_daily", "ingredient_id", "03_ingredients", "ingredient_id"),
    _fk("10_inventory_daily", "date", "07_calendar", "date", "date"),
    _fk("11_purchase_orders", "store_id", "01_stores", "store_id"),
    _fk("11_purchase_orders", "supplier_id", "05_suppliers", "supplier_id"),
    _fk("11_purchase_orders", "ingredient_id", "03_ingredients", "ingredient_id"),
    _fk("11_purchase_orders", "order_date", "07_calendar", "date", "date"),
    _fk("12_receipts", "po_line_id", "11_purchase_orders", "po_line_id"),
    _fk("12_receipts", "po_id", "11_purchase_orders", "po_id"),
    _fk("12_receipts", "store_id", "01_stores", "store_id"),
    _fk("12_receipts", "ingredient_id", "03_ingredients", "ingredient_id"),
    _fk("12_receipts", "receipt_date", "07_calendar", "date", "date"),
    _fk("13_waste", "store_id", "01_stores", "store_id"),
    _fk("13_waste", "ingredient_id", "03_ingredients", "ingredient_id"),
    _fk("13_waste", "date", "07_calendar", "date", "date"),
    _fk("14_transfers", "from_store_id", "01_stores", "store_id"),
    _fk("14_transfers", "to_store_id", "01_stores", "store_id"),
    _fk("14_transfers", "ingredient_id", "03_ingredients", "ingredient_id"),
    _fk("14_transfers", "date", "07_calendar", "date", "date"),
    _fk("15_menu_availability", "store_id", "01_stores", "store_id"),
    _fk("15_menu_availability", "menu_id", "02_menus", "menu_id"),
    _fk("16_inventory_lots_daily", "store_id", "01_stores", "store_id"),
    _fk("16_inventory_lots_daily", "ingredient_id", "03_ingredients", "ingredient_id"),
    _fk("16_inventory_lots_daily", "date", "07_calendar", "date", "date"),
    _fk("17_stockouts", "store_id", "01_stores", "store_id"),
    _fk("17_stockouts", "ingredient_id", "03_ingredients", "ingredient_id"),
    _fk("17_stockouts", "affected_menus", "02_menus", "menu_id", "list"),
    _fk("17_stockouts", "date", "07_calendar", "date", "date"),
)

# 실제 CSV 컬럼명이 명세와 다를 때 여기에 추가한다: {파일 키: {실제 컬럼명: 명세 컬럼명}}
# 대소문자·공백·camelCase 차이와 철자가 비슷한 이름(유사도 ≥ 0.85)은 loader 가 자동으로 맞추므로 그 밖의 경우만 적는다.
COLUMN_ALIASES: dict[str, dict[str, str]] = {
    # 예시: "13_waste": {"waste_date": "date"},
}

# 단위 일관성 검사 대상: use_unit / order_unit 이 들어 있는 테이블
USE_UNIT_TABLES: tuple[str, ...] = (
    "03_ingredients", "04_bom", "10_inventory_daily", "13_waste", "14_transfers", "16_inventory_lots_daily",
)
ORDER_UNIT_TABLES: tuple[str, ...] = ("03_ingredients", "11_purchase_orders", "12_receipts")

# 기간(DATA_START~DATA_END) 밖 값이 정상일 수 있는 날짜 컬럼 (입고 예정일, 사용기한, 단가 시작일, 기간형 마스터 등)
DATE_COLUMNS_ALLOWED_OUTSIDE: frozenset[str] = frozenset({
    "11_purchase_orders.expected_date",
    "16_inventory_lots_daily.receipt_date",
    "16_inventory_lots_daily.last_use_date",
    "06_ingredient_price_history.valid_from",
    "15_menu_availability.start_date",
    "15_menu_availability.end_date",
    "08_promotions.start_date",
    "08_promotions.end_date",
    "07_calendar.date",
})


def get_spec(key: str) -> TableSpec:
    """파일 키(또는 별칭)로 TableSpec 을 돌려준다. 없으면 KeyError."""
    resolved = resolve_table_key(key)
    if resolved is None:
        raise KeyError(f"알 수 없는 테이블: {key}. 가능한 키: {', '.join(TABLE_KEYS)}")
    return TABLE_BY_KEY[resolved]


def resolve_table_key(name: str) -> Optional[str]:
    """'09_sales', '09', '9', 'sales', '09_sales.csv' 를 모두 '09_sales' 로 바꾼다. 모르면 None."""
    if name is None:
        return None
    s = str(name).strip()
    if s.lower().endswith(".csv"):
        s = s[:-4]
    if s in TABLE_BY_KEY:
        return s
    if s.isdigit():
        n = int(s)
        for t in TABLES:
            if t.number == n:
                return t.key
        return None
    low = s.lower()
    for t in TABLES:
        if low == t.short_name:
            return t.key
    return None
