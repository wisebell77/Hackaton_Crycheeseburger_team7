"""
문자열 파싱 도우미. 로더(loader.py)와 테스트(tests/test_parsers.py)가 함께 쓴다.

실제 CSV의 표기 방식(목록 구분자, 요일 표기, 시각 표기)은 당일에야 알 수 있으므로
여러 변형을 모두 받아들이고, 무엇을 골랐는지 돌려주어 보고서에 남길 수 있게 만든다.

- 목록 컬럼   : detect_list_delimiter(values) → 구분자, parse_list_cell(value, delimiter) → list[str]
- 요일        : normalize_weekdays(tokens) → list[int] (ISO 1=월 … 7=일)
               normalize_weekdays_verbose(tokens, scheme) → (요일 목록, 해석 못 한 토큰)
               detect_numeric_weekday_scheme(tokens), infer_numeric_scheme_from_dates(dates, tokens)
- 시각/시간대 : parse_hhmm(value) → 자정 기준 분, parse_hour(value) → 시
- 형 변환     : coerce_date / coerce_numeric / coerce_flag01 / coerce_yn / clean_id / clean_text
               (모두 (새 Series, 경고 문자열 목록) 을 돌려준다. 입력 Series 는 바꾸지 않는다.)
- 컬럼명      : normalize_colname(name)
"""
from __future__ import annotations

import math
import re
from typing import Any, Iterable, Optional

import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# 공통
# ---------------------------------------------------------------------------

def is_missing(value: Any) -> bool:
    """None, NaN, pd.NA, NaT, 빈 문자열이면 True."""
    if value is None:
        return True
    if isinstance(value, (list, tuple, set, dict, np.ndarray)):
        return False
    try:
        if pd.isna(value):
            return True
    except (TypeError, ValueError):
        return False
    return isinstance(value, str) and value.strip() == ""


def examples(values: Iterable[Any], k: int = 5) -> str:
    """경고 메시지용 예시 문자열 (중복 제거, 최대 k개)."""
    seen: list[str] = []
    for v in values:
        s = repr(v) if not isinstance(v, str) else f"'{v}'"
        if s not in seen:
            seen.append(s)
        if len(seen) >= k:
            break
    return ", ".join(seen)


def normalize_colname(name: Any) -> str:
    """컬럼명 정규화: BOM/공백 제거, camelCase → snake_case, 소문자, 특수문자 → '_'.

    예: ' Store ID ' → 'store_id', 'storeId' → 'store_id', 'last-use-date' → 'last_use_date'
    """
    s = str(name).replace("﻿", "").strip()
    s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", s)
    s = s.lower()
    s = re.sub(r"[^0-9a-z가-힣]+", "_", s)
    s = re.sub(r"_+", "_", s).strip("_")
    return s


# ---------------------------------------------------------------------------
# 목록 컬럼 (set_components, target_menu, affected_menus, order_days, delivery_days)
# ---------------------------------------------------------------------------
WHITESPACE = "whitespace"
LIST_DELIMITERS: tuple[str, ...] = (",", ";", "|", "/", "+", "·", "、")
DELIMITER_NAMES: dict[Optional[str], str] = {
    ",": "comma(,)", ";": "semicolon(;)", "|": "pipe(|)", "/": "slash(/)", "+": "plus(+)",
    "·": "middle dot(·)", "、": "ideographic comma(、)", WHITESPACE: "whitespace(공백)",
    None: "none(구분자 없음: 모든 값이 원소 1개)",
}
EMPTY_TOKENS = {"", "-", "–", "—", "없음", "해당없음", "none", "null", "nan", "na", "n/a", "[]", "x"}
_SECONDARY_SPLIT = re.compile(r"[,;|]")
_RANGE_SPACES = re.compile(r"\s*([~\-–])\s*")


def delimiter_name(delimiter: Optional[str]) -> str:
    return DELIMITER_NAMES.get(delimiter, repr(delimiter))


def _strip_wrapping(text: str) -> str:
    """바깥 따옴표/괄호를 반복해서 벗긴다. 예: '"[M01, M02]"' → 'M01, M02'."""
    s = text.strip()
    pairs = {"[": "]", "(": ")", "{": "}"}
    changed = True
    while changed and len(s) >= 2:
        changed = False
        if s[0] in "\"'`" and s[-1] == s[0]:
            s = s[1:-1].strip()
            changed = True
        elif s[0] in pairs and s[-1] == pairs[s[0]]:
            s = s[1:-1].strip()
            changed = True
    return s


def _clean_token(token: str) -> str:
    t = token.strip().strip("\"'`").strip()
    t = t.strip("[](){}").strip()
    t = t.strip("\"'`").strip()
    return t


def _prepare_list_text(value: Any) -> str:
    s = _strip_wrapping(str(value))
    return _RANGE_SPACES.sub(r"\1", s)  # 'Mon - Fri' → 'Mon-Fri' (범위 표기 보호)


def detect_list_delimiter(values: Iterable[Any], sample: int = 20000) -> Optional[str]:
    """목록 컬럼 전체를 보고 가장 많이 쓰인 구분자를 고른다.

    후보: , ; | / + · 、 (공백은 다른 후보가 하나도 없을 때만). 아무것도 없으면 None.
    동률이면 위 순서(쉼표 우선)를 따른다.
    """
    counts = {d: 0 for d in LIST_DELIMITERS}
    ws = 0
    n = 0
    for v in values:
        if is_missing(v) or isinstance(v, (list, tuple)):
            continue
        s = _prepare_list_text(v)
        n += 1
        for d in LIST_DELIMITERS:
            if d in s:
                counts[d] += 1
        if re.search(r"\s", s):
            ws += 1
        if n >= sample:
            break
    best = max(LIST_DELIMITERS, key=lambda d: counts[d])
    if counts[best] > 0:
        return best
    if ws > 0:
        return WHITESPACE
    return None


def parse_list_cell(value: Any, delimiter: Optional[str] = ",") -> list[str]:
    """목록 셀 하나를 list[str] 로 바꾼다.

    - 바깥 괄호/따옴표 제거: "[M01, M02]", "['M01','M02']", '{M01;M02}' 모두 처리
    - delimiter 로 나눈 뒤, 남은 쉼표/세미콜론/파이프(섞어 쓴 경우)와 공백으로 한 번 더 나눈다
    - 빈 토큰과 '-', '없음', 'none', 'nan' 같은 빈 표시는 버린다
    - 결측(None/NaN/빈 문자열)은 [] 를 돌려준다
    """
    if isinstance(value, (list, tuple)):
        tokens = [str(v) for v in value]
    elif is_missing(value):
        return []
    else:
        s = _prepare_list_text(value)
        if delimiter is None:
            tokens = [s]
        elif delimiter == WHITESPACE:
            tokens = s.split()
        else:
            tokens = s.split(delimiter)
    out: list[str] = []
    for tok in tokens:
        for piece in _SECONDARY_SPLIT.split(tok):
            for sub in piece.split():
                c = _clean_token(sub)
                if c.lower() in EMPTY_TOKENS:
                    continue
                out.append(c)
    return out


# ---------------------------------------------------------------------------
# 요일
# ---------------------------------------------------------------------------
KOR_DAYS: dict[str, int] = {"월": 1, "화": 2, "수": 3, "목": 4, "금": 5, "토": 6, "일": 7}
ENG_DAYS: dict[str, int] = {
    "mon": 1, "monday": 1, "tue": 2, "tues": 2, "tuesday": 2, "wed": 3, "weds": 3, "wednesday": 3,
    "thu": 4, "thur": 4, "thurs": 4, "thursday": 4, "fri": 5, "friday": 5,
    "sat": 6, "saturday": 6, "sun": 7, "sunday": 7,
}
GROUP_WORDS: dict[str, list[int]] = {
    "매일": [1, 2, 3, 4, 5, 6, 7], "전체": [1, 2, 3, 4, 5, 6, 7], "everyday": [1, 2, 3, 4, 5, 6, 7],
    "daily": [1, 2, 3, 4, 5, 6, 7], "all": [1, 2, 3, 4, 5, 6, 7], "평일": [1, 2, 3, 4, 5],
    "weekdays": [1, 2, 3, 4, 5], "주말": [6, 7], "weekend": [6, 7], "weekends": [6, 7],
}
ISO_NAMES_KO: dict[int, str] = {1: "월", 2: "화", 3: "수", 4: "목", 5: "금", 6: "토", 7: "일"}

# 숫자 요일 체계 → ISO 변환표
NUMERIC_SCHEMES: dict[str, dict[int, int]] = {
    "mon0": {0: 1, 1: 2, 2: 3, 3: 4, 4: 5, 5: 6, 6: 7},   # 0=월 … 6=일 (파이썬 weekday, pandas dt.weekday)
    "sun0": {0: 7, 1: 1, 2: 2, 3: 3, 4: 4, 5: 5, 6: 6},   # 0=일 … 6=토 (JavaScript, cron)
    "iso": {1: 1, 2: 2, 3: 3, 4: 4, 5: 5, 6: 6, 7: 7},    # 1=월 … 7=일 (ISO 8601)
    "sun1": {1: 7, 2: 1, 3: 2, 4: 3, 5: 4, 6: 5, 7: 6},   # 1=일 … 7=토 (Excel WEEKDAY, SQL)
}
NUMERIC_SCHEME_DESC: dict[str, str] = {
    "mon0": "0=월 … 6=일 (파이썬 weekday)",
    "sun0": "0=일 … 6=토 (JavaScript/cron)",
    "iso": "1=월 … 7=일 (ISO)",
    "sun1": "1=일 … 7=토 (Excel/SQL)",
}
_NUMERIC_TOKEN = re.compile(r"^\d(?:\.0+)?$")


def is_numeric_weekday_token(token: Any) -> bool:
    return bool(_NUMERIC_TOKEN.match(str(token).strip()))


def detect_numeric_weekday_scheme(tokens: Iterable[Any]) -> tuple[str, str]:
    """숫자 요일 토큰만 보고 체계를 추정한다. (scheme, 설명) 을 돌려준다.

    0 이 있으면 0~6 체계(기본 0=월, 파이썬 관례), 7 이 있으면 1~7 체계(기본 1=월, ISO),
    둘 다 없으면 1~7(ISO)로 가정하되 '불확실' 로 표시한다. 달력으로 확정할 수 있으면
    infer_numeric_scheme_from_dates 를 쓴다.
    """
    nums = {int(float(str(t).strip())) for t in tokens if is_numeric_weekday_token(t)}
    if not nums:
        return "iso", "숫자 토큰 없음"
    if 0 in nums and 7 in nums:
        return "mon0", "0과 7이 함께 있어 판단 불가 → 0=월(파이썬) 가정, 확인 필요"
    if 0 in nums:
        return "mon0", "0이 있어 0~6 체계로 판단 → 0=월(파이썬) 가정 (0=일일 수도 있음, 발주일 대조 결과 확인)"
    if 7 in nums:
        return "iso", "7이 있어 1~7 체계로 판단 → 1=월(ISO) 가정 (1=일일 수도 있음, 발주일 대조 결과 확인)"
    return "iso", "0도 7도 없어 1~7(1=월, ISO)로 가정 — 불확실, 발주일 대조 결과 확인"


def infer_numeric_scheme_from_dates(dates: pd.Series, tokens: pd.Series) -> tuple[str, float]:
    """달력처럼 날짜와 숫자 요일이 같이 있으면 실제 요일과 가장 잘 맞는 체계를 고른다.

    반환: (scheme, 일치율 0~1)
    """
    d = pd.to_datetime(dates, errors="coerce")
    iso_actual = d.dt.isocalendar().day.astype("float")
    nums = pd.to_numeric(tokens.astype(str).str.strip(), errors="coerce")
    best, best_rate = "iso", -1.0
    for name, mapping in NUMERIC_SCHEMES.items():
        mapped = nums.map(lambda n: mapping.get(int(n)) if pd.notna(n) else np.nan).astype("float")
        valid = nums.notna() & iso_actual.notna()  # 체계에 없는 숫자(예: 0~6 체계의 7)는 불일치로 센다
        rate = float((mapped[valid] == iso_actual[valid]).mean()) if valid.any() else 0.0
        if rate > best_rate:
            best, best_rate = name, rate
    return best, best_rate


def _single_day(token: str, numeric_scheme: Optional[str]) -> Optional[int]:
    t = token.strip().strip("()[]{}").strip()
    if not t:
        return None
    if t in KOR_DAYS:
        return KOR_DAYS[t]
    if t.endswith("요일") and t[:-2] in KOR_DAYS:
        return KOR_DAYS[t[:-2]]
    low = t.lower().rstrip(".")
    if low in ENG_DAYS:
        return ENG_DAYS[low]
    if is_numeric_weekday_token(t):
        n = int(float(t))
        return NUMERIC_SCHEMES[numeric_scheme or "iso"].get(n)
    return None


def _day_range(a: int, b: int) -> list[int]:
    """a~b (양끝 포함). 토~월 처럼 거꾸로면 주를 넘겨 감싼다."""
    if a <= b:
        return list(range(a, b + 1))
    return list(range(a, 8)) + list(range(1, b + 1))


def expand_weekday_token(token: Any, numeric_scheme: Optional[str] = None) -> Optional[list[int]]:
    """토큰 하나를 ISO 요일 목록으로 바꾼다. 해석 못 하면 None.

    지원: 월/월요일/Mon/Monday/숫자, 범위(월~금, Mon-Fri), 묶음어(매일, 평일, 주말, daily),
    붙여 쓴 한글(월수금), '-' 로 3개 이상 이은 목록(월-수-금).
    """
    t = str(token).strip()
    if not t:
        return []
    low = t.lower()
    if t in GROUP_WORDS:
        return list(GROUP_WORDS[t])
    if low in GROUP_WORDS:
        return list(GROUP_WORDS[low])
    single = _single_day(t, numeric_scheme)
    if single is not None:
        return [single]
    parts = [p for p in re.split(r"[~\-–]", t) if p.strip()]
    if len(parts) == 2 and ("~" in t or "-" in t or "–" in t):
        a, b = _single_day(parts[0], numeric_scheme), _single_day(parts[1], numeric_scheme)
        if a is not None and b is not None:
            return _day_range(a, b)
    if len(parts) >= 3:
        days = [_single_day(p, numeric_scheme) for p in parts]
        if all(d is not None for d in days):
            return [int(d) for d in days if d is not None]
    compact = t.replace("요일", "")
    if compact and all(ch in KOR_DAYS for ch in compact):
        return [KOR_DAYS[ch] for ch in compact]
    return None


def normalize_weekdays_verbose(
    tokens: Any, numeric_scheme: Optional[str] = None
) -> tuple[list[int], list[str]]:
    """요일 토큰들을 ISO 요일(1=월 … 7=일) 정렬 목록으로 바꾸고, 해석 못 한 토큰도 돌려준다.

    tokens: list/tuple (예: ['월','수']), 문자열 하나(예: '월,수,금' → 자동 분할), 숫자 목록.
    numeric_scheme: 'mon0' | 'sun0' | 'iso' | 'sun1' | None(토큰을 보고 추정).
    """
    if isinstance(tokens, str):
        tokens = parse_list_cell(tokens, detect_list_delimiter([tokens]))
    elif tokens is None or (not isinstance(tokens, (list, tuple, set, np.ndarray)) and is_missing(tokens)):
        return [], []
    elif not isinstance(tokens, (list, tuple, set, np.ndarray)):
        tokens = [tokens]
    toks = [str(int(t)) if isinstance(t, (int, np.integer)) else str(t) for t in tokens]
    if numeric_scheme is None and any(is_numeric_weekday_token(t) for t in toks):
        numeric_scheme, _ = detect_numeric_weekday_scheme(toks)
    days: set[int] = set()
    unknown: list[str] = []
    for t in toks:
        expanded = expand_weekday_token(t, numeric_scheme)
        if expanded is None:
            unknown.append(t)
        else:
            days.update(expanded)
    return sorted(days), unknown


def normalize_weekdays(tokens: Any, numeric_scheme: Optional[str] = None) -> list[int]:
    """요일 토큰 → ISO 요일 정수 목록 (1=월 … 7=일, 정렬·중복 제거).

    지원 표기: 월화수목금토일, 월요일, Mon/Tue/…, Monday/…, 0~6(기본 0=월), 1~7(기본 1=월),
    월~금 / Mon-Fri 범위, 매일/평일/주말, 붙여 쓴 '월수금'. 해석 못 한 토큰은 버린다
    (버린 토큰이 궁금하면 normalize_weekdays_verbose 사용).

    예: normalize_weekdays(['월', '수', '금']) == [1, 3, 5]
        normalize_weekdays('Mon|Wed') == [1, 3]
        normalize_weekdays(['0', '2'], 'mon0') == [1, 3]
    """
    days, _ = normalize_weekdays_verbose(tokens, numeric_scheme)
    return days


def weekday_names(days: Iterable[int]) -> str:
    """[1, 3, 5] → '월,수,금' (보고서 표시용)."""
    return ",".join(ISO_NAMES_KO.get(int(d), str(d)) for d in days)


# ---------------------------------------------------------------------------
# 시각/시간대
# ---------------------------------------------------------------------------
_HHMM = re.compile(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?(?:\.\d+)?\s*([AaPp][Mm])?$")
_KOR_TIME = re.compile(r"^(오전|오후)?\s*(\d{1,2})\s*시\s*(?:(\d{1,2})\s*분)?\s*(?:까지|전)?$")
_DIGITS = re.compile(r"^\d{1,4}$")


def _valid_minutes(h: int, m: int) -> Optional[int]:
    if 0 <= h <= 24 and 0 <= m < 60 and h * 60 + m <= 24 * 60:
        return h * 60 + m
    return None


def parse_hhmm(value: Any) -> Optional[int]:
    """'HH:MM' 형식 시각을 자정 기준 분(int)으로. 해석 못 하면 None.

    지원: '11:00', '9:30', '11:00:00', '2:30 PM', '1100', '930', '11', '11시', '오후 2시 30분',
    '2026-01-01 11:00:00'(뒤의 시각만 사용). 숫자 11 → 660, 1100 → 660.
    """
    if is_missing(value):
        return None
    if isinstance(value, (int, float, np.integer, np.floating)) and not isinstance(value, bool):
        v = float(value)
        if not v.is_integer():
            return None
        n = int(v)
        if 0 <= n <= 24:
            return n * 60
        if 100 <= n <= 2400:
            return _valid_minutes(n // 100, n % 100)
        return None
    s = str(value).strip()
    m = _HHMM.match(s)
    if m:
        h, mi = int(m.group(1)), int(m.group(2))
        ampm = m.group(4)
        if ampm:
            h = h % 12 + (12 if ampm.lower() == "pm" else 0)
        return _valid_minutes(h, mi)
    m = _KOR_TIME.match(s)
    if m:
        h, mi = int(m.group(2)), int(m.group(3) or 0)
        if m.group(1) == "오후" and h < 12:
            h += 12
        if m.group(1) == "오전" and h == 12:
            h = 0
        return _valid_minutes(h, mi)
    if _DIGITS.match(s):
        return parse_hhmm(int(s))
    m = re.search(r"(\d{1,2}):(\d{2})(?::\d{2})?\s*$", s)
    if m:
        return _valid_minutes(int(m.group(1)), int(m.group(2)))
    return None


def minutes_to_hhmm(minutes: Optional[float]) -> str:
    """660 → '11:00'. 결측이면 ''."""
    if minutes is None or (isinstance(minutes, float) and math.isnan(minutes)):
        return ""
    m = int(minutes)
    return f"{m // 60:02d}:{m % 60:02d}"


_HOUR = re.compile(r"^(\d{1,2})(?:\.0+)?\s*(?::\d{2}(?::\d{2})?|시\s*대?|h)?\s*(?:[~\-]\s*\d{1,2}\s*시?)?$")


def parse_hour(value: Any) -> Optional[int]:
    """시간대 값을 정수 시로. '10', 10, 10.0, '10:00', '10시', '10시대', '10~11' → 10. 실패 시 None."""
    if is_missing(value):
        return None
    if isinstance(value, (int, float, np.integer, np.floating)) and not isinstance(value, bool):
        v = float(value)
        if v.is_integer() and 0 <= v <= 30:
            return int(v)
        return None
    m = _HOUR.match(str(value).strip())
    if m:
        h = int(m.group(1))
        return h if 0 <= h <= 30 else None
    return None


# ---------------------------------------------------------------------------
# Series 형 변환 (모두 새 Series 와 경고 목록을 돌려준다)
# ---------------------------------------------------------------------------

def _text(s: pd.Series) -> pd.Series:
    """결측은 그대로 두고 나머지를 공백 제거한 문자열로 (object dtype)."""
    out = s.astype(object).where(s.notna(), None)
    return out.map(lambda v: v.strip() if isinstance(v, str) else (None if v is None else str(v).strip()))


def _as_int_or_float(num: pd.Series) -> pd.Series:
    """결측이 없고 모두 정수면 int64, 아니면 float64."""
    if num.notna().all() and len(num) > 0 and np.all(np.mod(num.to_numpy(dtype=float), 1) == 0):
        return num.astype("int64")
    return num.astype("float64")


def clean_text(s: pd.Series) -> tuple[pd.Series, list[str]]:
    """str/category 컬럼: 앞뒤 공백 제거, 빈 문자열은 결측(None)으로."""
    out = _text(s).map(lambda v: None if v == "" else v)
    return out, []


def clean_id(s: pd.Series) -> tuple[pd.Series, list[str]]:
    """ID 컬럼: 문자열로 통일하고 앞뒤 공백 제거. 모든 값이 '12.0' 꼴이면 '12' 로 정리."""
    issues: list[str] = []
    if pd.api.types.is_float_dtype(s):
        vals = s.dropna()
        if len(vals) and np.all(np.mod(vals.to_numpy(), 1) == 0):
            s = s.map(lambda v: None if pd.isna(v) else str(int(v)))
            issues.append("숫자형 ID 를 정수 문자열로 변환")
    out = _text(s).map(lambda v: None if v == "" else v)
    nonnull = out.dropna()
    if len(nonnull) and nonnull.str.fullmatch(r"-?\d+\.0+").all():
        out = out.map(lambda v: re.sub(r"\.0+$", "", v) if isinstance(v, str) else v)
        issues.append("ID 가 모두 '12.0' 형태여서 '.0' 을 제거함")
    return out, issues


def coerce_numeric(s: pd.Series, integer: bool = False) -> tuple[pd.Series, list[str]]:
    """숫자 컬럼 변환. 천 단위 쉼표/공백 제거 후 to_numeric. 실패 값은 NaN + 경고."""
    issues: list[str] = []
    if pd.api.types.is_bool_dtype(s):
        num = s.astype("float64")
    elif pd.api.types.is_numeric_dtype(s):
        num = s.astype("float64")
    else:
        txt = _text(s)
        cleaned = txt.map(lambda v: v.replace(",", "").replace(" ", "") if isinstance(v, str) else v)
        cleaned = cleaned.map(lambda v: None if v in ("", "-", "nan", "NaN", "None", "null") else v)
        num = pd.to_numeric(cleaned, errors="coerce").astype("float64")
        bad = num.isna() & cleaned.notna()
        if bad.any():
            issues.append(f"숫자 변환 실패 {int(bad.sum())}건 → NaN (예: {examples(txt[bad])})")
    if integer:
        vals = num.dropna().to_numpy()
        if len(vals) and not np.all(np.mod(vals, 1) == 0):
            issues.append(f"정수 기대 컬럼에 소수 값 {int((np.mod(vals, 1) != 0).sum())}건 → float 유지")
            return num, issues
        if num.isna().any() and len(num):
            issues.append(f"결측 {int(num.isna().sum())}건이 있어 float64 로 유지")
            return num, issues
        return _as_int_or_float(num), issues
    return num, issues


_FLAG_MAP = {"1": 1, "0": 0, "1.0": 1, "0.0": 0, "true": 1, "false": 0, "y": 1, "n": 0,
             "yes": 1, "no": 0, "t": 1, "f": 0}
_YN_MAP = {"y": True, "n": False, "yes": True, "no": False, "1": True, "0": False, "1.0": True,
           "0.0": False, "o": True, "x": False, "true": True, "false": False, "t": True, "f": False,
           "운영": True, "미운영": False, "예": True, "아니오": False, "가능": True, "불가": False}


def coerce_flag01(s: pd.Series) -> tuple[pd.Series, list[str]]:
    """0/1 플래그 → 0/1 정수. 'Y/N', 'True/False' 도 받아들인다."""
    issues: list[str] = []
    if pd.api.types.is_numeric_dtype(s) and not pd.api.types.is_bool_dtype(s):
        num = s.astype("float64")
        bad = num.notna() & ~num.isin([0.0, 1.0])
        if bad.any():
            issues.append(f"0/1 이 아닌 값 {int(bad.sum())}건 → NaN (예: {examples(num[bad])})")
            num = num.where(~bad)
    else:
        txt = _text(s)
        low = txt.map(lambda v: v.lower() if isinstance(v, str) else v)
        num = low.map(lambda v: _FLAG_MAP.get(v, np.nan) if isinstance(v, str) and v != "" else np.nan)
        num = num.astype("float64")
        bad = num.isna() & low.map(lambda v: isinstance(v, str) and v != "")
        if bad.any():
            issues.append(f"0/1 로 해석 못 한 값 {int(bad.sum())}건 → NaN (예: {examples(txt[bad])})")
    if num.isna().any():
        if len(num) and num.isna().all():
            issues.append("모든 값이 결측")
        return num, issues
    return num.astype("int64"), issues


def coerce_yn(s: pd.Series) -> tuple[pd.Series, list[str]]:
    """Y/N 여부 → bool. 결측이나 해석 못 한 값이 있으면 pandas 'boolean'(nullable)."""
    issues: list[str] = []
    txt = _text(s)
    low = txt.map(lambda v: v.lower() if isinstance(v, str) else v)
    mapped = low.map(lambda v: _YN_MAP.get(v) if isinstance(v, str) else None)
    bad = mapped.isna() & low.map(lambda v: isinstance(v, str) and v != "")
    if bad.any():
        issues.append(f"Y/N 으로 해석 못 한 값 {int(bad.sum())}건 → 결측 (예: {examples(txt[bad])})")
    if mapped.isna().any():
        return mapped.astype("boolean"), issues
    return mapped.astype(bool), issues


_ALT_DATE_FORMATS = ("%Y/%m/%d", "%Y.%m.%d", "%Y%m%d", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S",
                     "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M:%S", "%Y. %m. %d")


def coerce_date(s: pd.Series) -> tuple[pd.Series, list[str]]:
    """날짜 컬럼 → datetime64[ns] (자정으로 정규화). 기본 형식 YYYY-MM-DD, 실패 값만 다른 형식 재시도.

    '9999-12-31' 같은 범위 밖 날짜는 NaT 가 되며 별도 경고로 알린다(기간 무기한으로 해석 권장).
    """
    issues: list[str] = []
    if pd.api.types.is_datetime64_any_dtype(s):
        return pd.to_datetime(s).dt.normalize(), issues
    txt = _text(s).map(lambda v: None if v == "" else v)
    out = pd.to_datetime(txt, format="%Y-%m-%d", errors="coerce")
    bad = out.isna() & txt.notna()
    used: list[str] = []
    if bad.any():
        for fmt in _ALT_DATE_FORMATS:
            alt = pd.to_datetime(txt[bad], format=fmt, errors="coerce")
            ok = alt.notna()
            if ok.any():
                out = out.copy()
                out.loc[alt.index[ok]] = alt[ok]
                used.append(fmt)
                bad = out.isna() & txt.notna()
            if not bad.any():
                break
    if used:
        issues.append(f"YYYY-MM-DD 가 아닌 날짜 형식 사용: {', '.join(used)}")
    if bad.any():
        far = txt[bad].map(lambda v: bool(re.match(r"^(2[3-9]|[3-9]\d)\d\d-\d{2}-\d{2}", str(v))))
        n_far = int(far.sum())
        if n_far:
            issues.append(f"pandas 범위 밖 먼 미래 날짜 {n_far}건 → NaT (예: {examples(txt[bad][far])}). "
                          "기간 종료일이면 '무기한'으로 해석")
        n_other = int(bad.sum()) - n_far
        if n_other:
            issues.append(f"날짜 해석 실패 {n_other}건 → NaT (예: {examples(txt[bad][~far])})")
    return out.dt.normalize(), issues


def coerce_hour(s: pd.Series) -> tuple[pd.Series, list[str]]:
    """시간대 컬럼 → 정수 시 (결측 있으면 float64)."""
    issues: list[str] = []
    vals = s.map(parse_hour)
    bad = vals.isna() & s.map(lambda v: not is_missing(v))
    if bad.any():
        issues.append(f"시간대 해석 실패 {int(bad.sum())}건 → NaN (예: {examples(s[bad])})")
    num = pd.to_numeric(vals, errors="coerce").astype("float64")
    return _as_int_or_float(num), issues


def coerce_hhmm(s: pd.Series) -> tuple[pd.Series, list[str]]:
    """HH:MM 컬럼 → 자정 기준 분 Series (정수, 결측 있으면 float64). 원본은 호출 측에서 보관."""
    issues: list[str] = []
    vals = s.map(parse_hhmm)
    bad = vals.isna() & s.map(lambda v: not is_missing(v))
    if bad.any():
        issues.append(f"시각(HH:MM) 해석 실패 {int(bad.sum())}건 → NaN (예: {examples(s[bad])})")
    num = pd.to_numeric(vals, errors="coerce").astype("float64")
    return _as_int_or_float(num), issues
