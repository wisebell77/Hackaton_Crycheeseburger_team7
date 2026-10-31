"""
파서 단위 테스트 (plain assert). 두 가지 방법으로 실행할 수 있다.
    python -m pytest tests/test_parsers.py -q
    python tests/test_parsers.py

실데이터의 표기가 fixture(한글 요일, 쉼표 목록)와 다를 수 있으므로 다른 변형(영문/숫자 요일, ; | 공백 구분자,
괄호·따옴표, cp949 인코딩, BOM, 따옴표 없는 목록, 번호 없는 파일명)을 여기서 확인한다.
임시 파일은 프로젝트의 data/_tmp_tests/ 아래에 만들고 끝나면 지운다.
"""
from __future__ import annotations

import shutil
import sys
import uuid
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd  # noqa: E402

from src import schema  # noqa: E402
from src.loader import find_csv_files, load_all, map_columns, repair_overflow_fields, resolve_files  # noqa: E402
from src.parsers import (coerce_date, coerce_flag01, coerce_hour, coerce_numeric, coerce_yn, clean_id,  # noqa: E402
                         detect_list_delimiter, detect_numeric_weekday_scheme, infer_numeric_scheme_from_dates,
                         normalize_colname, normalize_weekdays, normalize_weekdays_verbose, parse_hhmm, parse_hour,
                         parse_list_cell)

TMP_ROOT = PROJECT_ROOT / "data" / "_tmp_tests"


def _tmpdir() -> Path:
    d = TMP_ROOT / f"t_{uuid.uuid4().hex[:8]}"
    d.mkdir(parents=True)
    return d


def teardown_module(module: object) -> None:
    """pytest 가 모듈 테스트를 마친 뒤 임시 폴더를 지운다."""
    shutil.rmtree(TMP_ROOT, ignore_errors=True)


# ---------------------------------------------------------------------------
# 요일
# ---------------------------------------------------------------------------

def test_weekdays_korean() -> None:
    assert normalize_weekdays(["월", "수", "금"]) == [1, 3, 5]
    assert normalize_weekdays(["월요일", "목요일"]) == [1, 4]
    assert normalize_weekdays(["(화)", "토"]) == [2, 6]
    assert normalize_weekdays("월,수,금") == [1, 3, 5]          # 문자열 하나도 자동 분할
    assert normalize_weekdays("월수금") == [1, 3, 5]            # 붙여 쓴 한글
    assert normalize_weekdays(["일", "월"]) == [1, 7]           # 정렬
    assert normalize_weekdays(["월", "월"]) == [1]              # 중복 제거


def test_weekdays_english() -> None:
    assert normalize_weekdays(["Mon", "Wed", "Fri"]) == [1, 3, 5]
    assert normalize_weekdays(["monday", "THURSDAY"]) == [1, 4]
    assert normalize_weekdays(["Tue.", "Thurs", "Sun"]) == [2, 4, 7]
    assert normalize_weekdays("Mon|Thu") == [1, 4]


def test_weekdays_numeric_schemes() -> None:
    assert normalize_weekdays(["0", "3"], "mon0") == [1, 4]     # 0=월
    assert normalize_weekdays(["0", "3"], "sun0") == [3, 7]     # 0=일
    assert normalize_weekdays(["1", "7"], "iso") == [1, 7]      # 1=월 … 7=일
    assert normalize_weekdays(["1", "7"], "sun1") == [6, 7]     # 1=일 … 7=토
    assert normalize_weekdays([0, 3]) == [1, 4]                 # 0 이 있으면 자동으로 0~6(0=월)
    assert normalize_weekdays(["1", "7"]) == [1, 7]             # 7 이 있으면 자동으로 1~7(1=월)
    assert normalize_weekdays(["2.0", "4.0"], "iso") == [2, 4]  # 실수 표기
    assert detect_numeric_weekday_scheme(["0", "6"])[0] == "mon0"
    assert detect_numeric_weekday_scheme(["7"])[0] == "iso"


def test_weekdays_ranges_and_groups() -> None:
    assert normalize_weekdays("월~금") == [1, 2, 3, 4, 5]
    assert normalize_weekdays(["Mon-Fri"]) == [1, 2, 3, 4, 5]
    assert normalize_weekdays("Mon - Wed") == [1, 2, 3]
    assert normalize_weekdays(["토~월"]) == [1, 6, 7]           # 주를 넘는 범위
    assert normalize_weekdays(["월-수-금"]) == [1, 3, 5]        # '-' 를 3개 이상 이은 목록
    assert normalize_weekdays(["매일"]) == [1, 2, 3, 4, 5, 6, 7]
    assert normalize_weekdays(["평일"]) == [1, 2, 3, 4, 5]
    assert normalize_weekdays(["주말"]) == [6, 7]
    assert normalize_weekdays(["daily"]) == [1, 2, 3, 4, 5, 6, 7]


def test_weekdays_unknown_tokens_reported() -> None:
    days, unknown = normalize_weekdays_verbose(["월", "격주", "Fri"])
    assert days == [1, 5]
    assert unknown == ["격주"]
    assert normalize_weekdays(None) == []
    assert normalize_weekdays([]) == []


def test_infer_scheme_from_calendar_dates() -> None:
    dates = pd.Series(pd.date_range("2026-01-01", periods=21))
    py = pd.Series([str(d.weekday()) for d in dates])                     # 0=월
    js = pd.Series([str((d.weekday() + 1) % 7) for d in dates])           # 0=일
    iso = pd.Series([str(d.isoweekday()) for d in dates])                 # 1=월
    sun1 = pd.Series([str(d.isoweekday() % 7 + 1) for d in dates])        # 1=일
    assert infer_numeric_scheme_from_dates(dates, py) == ("mon0", 1.0)
    assert infer_numeric_scheme_from_dates(dates, js) == ("sun0", 1.0)
    assert infer_numeric_scheme_from_dates(dates, iso) == ("iso", 1.0)
    assert infer_numeric_scheme_from_dates(dates, sun1) == ("sun1", 1.0)


# ---------------------------------------------------------------------------
# 목록 컬럼
# ---------------------------------------------------------------------------

def test_detect_list_delimiter() -> None:
    assert detect_list_delimiter(["M01,M02", "M03"]) == ","
    assert detect_list_delimiter(["M01; M02", None, "M03"]) == ";"
    assert detect_list_delimiter(["M01 | M02"]) == "|"
    assert detect_list_delimiter(["M01 M02", "M03 M04"]) == "whitespace"
    assert detect_list_delimiter(["[M01, M02]", "['M03']"]) == ","
    assert detect_list_delimiter(["월/수/금"]) == "/"
    assert detect_list_delimiter(["M01+M06+M08"]) == "+"
    assert detect_list_delimiter(["M01", "M02"]) is None
    assert detect_list_delimiter([]) is None


def test_parse_list_cell_variants() -> None:
    expected = ["M01", "M06", "M08"]
    cases = [
        ("M01,M06,M08", ","), ("M01, M06, M08", ","), ('"M01,M06,M08"', ","),
        ("M01;M06;M08", ";"), ("M01 ; M06 ; M08", ";"), ("M01|M06|M08", "|"),
        ("M01 M06 M08", "whitespace"), ("[M01, M06, M08]", ","), ("['M01', 'M06', 'M08']", ","),
        ('["M01","M06","M08"]', ","), ("{M01;M06;M08}", ";"), ("(M01|M06|M08)", "|"),
        ("M01+M06+M08", "+"), ("M01,M06;M08", ","),                      # 섞어 쓴 구분자
    ]
    for raw, delim in cases:
        assert parse_list_cell(raw, delim) == expected, (raw, delim, parse_list_cell(raw, delim))
        assert parse_list_cell(raw, detect_list_delimiter([raw])) == expected, raw
    for empty in (None, float("nan"), "", "-", "[]", "없음", "None"):
        assert parse_list_cell(empty, ",") == [], empty
    assert parse_list_cell("M01", None) == ["M01"]
    assert parse_list_cell(["M01", "M02"], ",") == ["M01", "M02"]


def test_list_then_weekdays_pipeline() -> None:
    variants = ["월,수,금", "월;수;금", "월|수|금", "월 수 금", "[월, 수, 금]", "'월','수','금'", "월/수/금", "월·수·금",
                "Mon,Wed,Fri", "mon|wed|fri", "Monday;Wednesday;Friday", '["Mon", "Wed", "Fri"]', "월요일, 수요일, 금요일"]
    for raw in variants:
        tokens = parse_list_cell(raw, detect_list_delimiter([raw]))
        assert normalize_weekdays(tokens) == [1, 3, 5], (raw, tokens)
    assert normalize_weekdays(parse_list_cell("0,2,4", ",")) == [1, 3, 5]   # 0=월 체계 자동
    assert normalize_weekdays(parse_list_cell("1|3|5|7", "|")) == [1, 3, 5, 7]


# ---------------------------------------------------------------------------
# 시각 / 시간대 / 형 변환
# ---------------------------------------------------------------------------

def test_parse_hhmm() -> None:
    cases = {"11:00": 660, "9:30": 570, "09:30": 570, "11:00:00": 660, "2:30 PM": 870, "12:00 AM": 0, "1100": 660,
             "930": 570, "11": 660, "11시": 660, "오후 2시 30분": 870, "오전 9시": 540, "2026-01-01 11:00:00": 660,
             "24:00": 1440}
    for raw, minutes in cases.items():
        assert parse_hhmm(raw) == minutes, (raw, parse_hhmm(raw))
    assert parse_hhmm(1100) == 660 and parse_hhmm(11.0) == 660
    for bad in (None, "", "abc", "25:00", "11:75", 11.5):
        assert parse_hhmm(bad) is None, bad


def test_parse_hour() -> None:
    for raw in ("10", 10, 10.0, "10:00", "10시", "10시대", "10~11", " 10 "):
        assert parse_hour(raw) == 10, raw
    for bad in (None, "", "x", 10.5, "99"):
        assert parse_hour(bad) is None, bad
    s, issues = coerce_hour(pd.Series(["10", "11:00", "bad"]))
    assert s.tolist()[:2] == [10.0, 11.0] and len(issues) == 1


def test_coerce_flags_and_yn() -> None:
    s, issues = coerce_flag01(pd.Series(["1", "0", "Y", "N", "true"]))
    assert s.tolist() == [1, 0, 1, 0, 1] and s.dtype == "int64" and not issues
    s, issues = coerce_flag01(pd.Series([0, 1, 2]))
    assert pd.isna(s.iloc[2]) and len(issues) == 1                        # 0/1 밖 값은 NaN + 경고
    s, issues = coerce_yn(pd.Series(["Y", "N", "y", "1", "O", "X", "운영", "미운영"]))
    assert s.tolist() == [True, False, True, True, True, False, True, False] and not issues
    s, issues = coerce_yn(pd.Series(["Y", "maybe"]))
    assert pd.isna(s.iloc[1]) and len(issues) == 1


def test_coerce_dates_numbers_ids() -> None:
    s, issues = coerce_date(pd.Series(["2026-01-01", "2026/01/02", "20260103", "9999-12-31", "bad", None]))
    assert s.iloc[0] == pd.Timestamp("2026-01-01") and s.iloc[1] == pd.Timestamp("2026-01-02")
    assert s.iloc[2] == pd.Timestamp("2026-01-03") and s.iloc[3:].isna().all()
    assert any("먼 미래" in m for m in issues) and any("해석 실패" in m for m in issues)
    n, issues = coerce_numeric(pd.Series(["1,234", "5", ""]), integer=False)
    assert n.iloc[0] == 1234.0 and n.iloc[1] == 5.0 and pd.isna(n.iloc[2]) and not issues
    n, issues = coerce_numeric(pd.Series(["1", "2"]), integer=True)
    assert n.dtype == "int64"
    n, issues = coerce_numeric(pd.Series(["1.5", "2"]), integer=True)
    assert n.dtype == "float64" and any("소수" in m for m in issues)
    ids, issues = clean_id(pd.Series(["1.0", "2.0", None]))
    assert ids.tolist()[:2] == ["1", "2"] and issues
    ids, _ = clean_id(pd.Series([" S01 ", "S02"]))
    assert ids.tolist() == ["S01", "S02"]


# ---------------------------------------------------------------------------
# 컬럼명 / 파일 찾기 / 줄 복구
# ---------------------------------------------------------------------------

def test_column_name_mapping() -> None:
    assert normalize_colname(" Store ID ") == "store_id"
    assert normalize_colname("storeId") == "store_id"
    assert normalize_colname("﻿date") == "date"
    assert normalize_colname("last-use-date") == "last_use_date"
    spec = schema.get_spec("16_inventory_lots_daily")
    mapping, renamed = map_columns(["Date", "store_id", "ingredientId", "lot_id", "receipt_dt", "last_use_date",
                                    "remaining_qty", "use_unit", "memo"], spec)
    assert mapping["Date"] == "date" and mapping["ingredientId"] == "ingredient_id"
    assert mapping["receipt_dt"] == "receipt_date" and "memo" not in mapping
    assert renamed == {"Date": "date", "ingredientId": "ingredient_id", "receipt_dt": "receipt_date"}


def test_repair_overflow_fields() -> None:
    kinds = [s.kind for s in schema.get_spec("02_menus").columns]
    fixed = repair_overflow_fields(["M11", "불고기세트", "세트", "1", "M01", "M06", "M08", "8500"], kinds)
    assert fixed == ["M11", "불고기세트", "세트", "1", "M01,M06,M08", "8500"]
    kinds = [s.kind for s in schema.get_spec("05_suppliers").columns]
    fixed = repair_overflow_fields(["SP01", "A", "월", "목", "14:00", "2", "수", "토", "100000", "메모"], kinds)
    assert fixed == ["SP01", "A", "월,목", "14:00", "2", "수,토", "100000", "메모"]
    assert repair_overflow_fields(["a", "b"], ["id", "str"]) is None


def test_resolve_files_by_prefix_header_and_name() -> None:
    d = _tmpdir()
    try:
        (d / "nested").mkdir()
        (d / "nested" / "9_sales.csv").write_text("date,hour,store_id,menu_id,channel,qty\n", encoding="utf-8")
        (d / "stores_master.csv").write_text("store_id,store_name,trade_area\n", encoding="utf-8")
        (d / "lots.csv").write_text("date,store_id,ingredient_id,lot_id,receipt_date,last_use_date,remaining_qty,use_unit\n",
                                    encoding="utf-8")
        (d / "inventory_daily.csv").write_text("date,store_id,ingredient_id,closing_qty\n", encoding="utf-8")
        (d / "readme_notes.csv").write_text("foo,bar\n1,2\n", encoding="utf-8")
        (d / "__MACOSX").mkdir()
        (d / "__MACOSX" / "._01_stores.csv").write_text("x", encoding="utf-8")
        csvs = find_csv_files(d)
        assert all("__MACOSX" not in str(p) for p in csvs)
        res, unmatched = resolve_files(csvs, d)
        assert res["09_sales"].path.name == "9_sales.csv" and res["09_sales"].method == "number_prefix"
        assert res["16_inventory_lots_daily"].path.name == "lots.csv"
        assert res["10_inventory_daily"].path.name == "inventory_daily.csv"
        assert res["01_stores"].path.name == "stores_master.csv"
        assert [p.name for p in unmatched] == ["readme_notes.csv"]
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_loader_encodings_and_variants() -> None:
    """cp949 / BOM / 영문 파이프 요일 / 숫자 달력 / 따옴표 없는 목록 / 컬럼명 차이를 로더가 처리하는지."""
    d = _tmpdir()
    try:
        sup = ("supplier_id,supplier_name,order_days,order_cutoff,lead_time_days,delivery_days,min_order_amount,note\n"
               "SP01,한빛냉동,Mon|Thu,2:00 PM,2,Wed|Sat,100000,메모\n"
               "SP02,새벽신선,[Mon; Tue; Wed],11:00,1,Tue~Sat,0,\n")
        (d / "05_suppliers.csv").write_bytes(sup.encode("cp949"))
        cal = "﻿Date,Weekday,is_holiday,holiday_name\n" + "".join(
            f"{day:%Y-%m-%d},{day.weekday()},0,\n" for day in pd.date_range("2026-01-01", periods=14))
        (d / "07_calendar.csv").write_text(cal, encoding="utf-8")
        menus = ("menu_id,menu_name,category,is_set,set_components,virtual_price\n"
                 "M01,버거,버거,0,,5000\nM11,세트,세트,1,M01,M06,M08,8500\nM06,감자,사이드,0,-,2000\n")
        (d / "02_menus.csv").write_text(menus, encoding="utf-8")
        ds = load_all(d)
        s = ds["05_suppliers"]
        assert ds.load_report["05_suppliers"].encoding == "cp949"
        assert s["order_days"].tolist() == [[1, 4], [1, 2, 3]]
        assert s["delivery_days"].tolist() == [[3, 6], [2, 3, 4, 5, 6]]
        assert s["order_cutoff_min"].tolist() == [840, 660]
        c = ds["07_calendar"]
        assert ds.load_report["07_calendar"].encoding == "utf-8-sig"
        assert ds.meta["weekday_scheme"]["numeric_scheme"] == "mon0"
        assert (c["weekday_iso"] == c["date"].dt.isocalendar().day).all()
        m = ds["02_menus"]
        assert ds.load_report["02_menus"].n_repaired_lines == 1
        assert m.set_index("menu_id").loc["M11", "set_components"] == ["M01", "M06", "M08"]
        assert m.set_index("menu_id").loc["M06", "set_components"] == []
        assert len(ds.missing_keys) == 14 and ds["09_sales"] is None
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------------------
# 스크립트 실행
# ---------------------------------------------------------------------------

def _run_as_script() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"PASS  {name}")
        except Exception as e:  # 실패 내용을 보여주고 계속
            failed += 1
            print(f"FAIL  {name}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} 통과")
    shutil.rmtree(TMP_ROOT, ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_run_as_script())
