"""
CSV 17개를 찾아 읽고, 스키마(schema.py)에 맞게 형 변환하는 로더.

사용법
    from src.loader import load_all
    ds = load_all("data/raw")            # 폴더 또는 .zip 경로
    sales = ds["09_sales"]               # DataFrame (파일이 없으면 None). ds["sales"], ds["09"] 도 가능
    info = ds.load_report["09_sales"]    # 경로, 행 수, 누락/추가 컬럼, 인코딩, 구분자, 경고
    ds.summary_frame()                   # 파일별 로딩 요약표

동작 요약
1. 파일 찾기: 폴더(하위 3단계까지, __MACOSX 제외)에서 CSV 를 모은다. 폴더에 CSV 없이 zip 만 있거나
   경로 자체가 zip 이면 data/_extracted/<zip이름>/ 에 풀어서 쓴다.
   - 1순위: 파일명 앞 번호 (예: '09' → ^0*9(?!\\d) 로 '09_sales.csv', '9_sales.csv' 매칭)
   - 2순위: 헤더 컬럼이 명세 컬럼과 겹치는 정도 (자카드 ≥ 0.5)
   - 3순위: 파일명 유사도 (≥ 0.75)
2. 읽기: utf-8 → utf-8-sig → cp949 순서로 시도. 구분자(, 탭 ; |)는 헤더 줄로 판단.
   칸 수가 맞지 않는 줄은 목록 컬럼을 따옴표 없이 쓴 경우로 보고 복구를 시도하며, 복구 못 한 줄은
   load_report 에 개수와 예시를 남긴다 (조용히 버리지 않는다).
3. 컬럼명 보정: 공백/대소문자/camelCase 차이는 자동 정규화, 비슷한 이름(유사도 ≥ 0.85)은 명세 이름으로 바꾸고
   renamed_columns 에 기록. 명세에 없는 컬럼은 그대로 둔다(extra_columns).
4. 형 변환: schema 의 kind 에 따라 변환 (날짜, HH:MM, 시간대, 숫자, 0/1, Y/N, 목록, 요일).
5. 요일: 공급사 order_days/delivery_days 와 달력 weekday 를 ISO 정수(1=월 … 7=일)로 바꾼다.
   숫자 표기면 달력 날짜와 대조해 체계(0=월/0=일/1=월/1=일)를 고르고 load_report 에 남긴다.

주의: ds 의 DataFrame 은 읽기 전용으로 다룬다. 값을 바꿀 때는 반드시 .copy() 또는 assign 을 쓴다.
"""
from __future__ import annotations

import codecs
import csv
import difflib
import re
import shutil
import time
import zipfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterator, Optional, Union

import numpy as np
import pandas as pd

from . import schema
from .parsers import (  # noqa: F401  (normalize_weekdays 는 loader 에서도 바로 쓸 수 있게 다시 내보낸다)
    NUMERIC_SCHEME_DESC,
    NUMERIC_SCHEMES,
    clean_id,
    clean_text,
    coerce_date,
    coerce_flag01,
    coerce_hhmm,
    coerce_hour,
    coerce_numeric,
    coerce_yn,
    delimiter_name,
    detect_list_delimiter,
    detect_numeric_weekday_scheme,
    expand_weekday_token,
    infer_numeric_scheme_from_dates,
    is_numeric_weekday_token,
    normalize_colname,
    normalize_weekdays,
    normalize_weekdays_verbose,
    parse_list_cell,
    weekday_names,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_EXTRACT_ROOT = PROJECT_ROOT / "data" / "_extracted"
ENCODINGS: tuple[str, ...] = ("utf-8", "utf-8-sig", "cp949")
CSV_SUFFIXES = (".csv", ".tsv")
MAX_SEARCH_DEPTH = 3
FUZZY_COLUMN_CUTOFF = 0.85
HEADER_OVERLAP_CUTOFF = 0.5
NAME_SIMILARITY_CUTOFF = 0.75
MAX_KEPT_BAD_LINES = 20

PathLike = Union[str, Path]


# ---------------------------------------------------------------------------
# 결과 자료형
# ---------------------------------------------------------------------------
@dataclass
class TableLoadInfo:
    """파일 하나의 로딩 기록. 보고서(load_report.md, validation_report.md)에 그대로 출력된다."""

    key: str
    expected_filename: str
    status: str = "missing"  # ok / missing / error
    path: Optional[str] = None
    match_method: Optional[str] = None
    other_candidates: list[str] = field(default_factory=list)
    encoding: Optional[str] = None
    csv_sep: Optional[str] = None
    rows: int = 0
    n_columns: int = 0
    columns_in_file: list[str] = field(default_factory=list)
    missing_columns: list[str] = field(default_factory=list)
    extra_columns: list[str] = field(default_factory=list)
    renamed_columns: dict[str, str] = field(default_factory=dict)
    list_delimiters: dict[str, str] = field(default_factory=dict)
    weekday_mapping: dict[str, Any] = field(default_factory=dict)
    parse_warnings: list[str] = field(default_factory=list)
    n_repaired_lines: int = 0
    n_bad_lines: int = 0
    bad_lines: list[str] = field(default_factory=list)
    memory_mb: float = 0.0
    seconds: float = 0.0
    error: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Dataset:
    """파일 키별 DataFrame 묶음 + 로딩 보고.

    tables      : {'01_stores': DataFrame 또는 None, ...} (17개 키가 항상 존재)
    load_report : {'01_stores': TableLoadInfo, ...}
    data_dir    : 실제로 읽은 폴더 (zip 이면 푼 폴더)
    source      : 사용자가 넘긴 경로
    warnings    : 데이터셋 전체 경고 (누락 파일, 알 수 없는 CSV 등)
    meta        : 요일 표기 판단 결과 등 부가 정보
    """

    tables: dict[str, Optional[pd.DataFrame]]
    load_report: dict[str, TableLoadInfo]
    data_dir: Path
    source: str
    warnings: list[str] = field(default_factory=list)
    unmatched_files: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)

    def __repr__(self) -> str:  # 노트북에서 ds 를 찍어도 표 전체가 쏟아지지 않게
        missing = ", ".join(self.missing_keys) or "없음"
        return f"Dataset({len(self.loaded_keys)}/{len(self.tables)}개 테이블, source='{self.source}', 누락: {missing})"

    def __getitem__(self, key: str) -> Optional[pd.DataFrame]:
        resolved = schema.resolve_table_key(key)
        if resolved is None:
            raise KeyError(f"알 수 없는 테이블 키: {key}. 가능한 키: {', '.join(schema.TABLE_KEYS)}")
        return self.tables.get(resolved)

    def get(self, key: str, default: Optional[pd.DataFrame] = None) -> Optional[pd.DataFrame]:
        resolved = schema.resolve_table_key(key)
        if resolved is None:
            return default
        df = self.tables.get(resolved)
        return default if df is None else df

    def __contains__(self, key: object) -> bool:
        return isinstance(key, str) and self.get(key) is not None

    def has(self, key: str, columns: tuple[str, ...] | list[str] = ()) -> bool:
        """테이블이 있고 columns 가 모두 있으면 True."""
        df = self.get(key)
        return df is not None and all(c in df.columns for c in columns)

    def missing_columns(self, key: str, columns: tuple[str, ...] | list[str]) -> list[str]:
        df = self.get(key)
        if df is None:
            return list(columns)
        return [c for c in columns if c not in df.columns]

    @property
    def loaded_keys(self) -> list[str]:
        return [k for k, v in self.tables.items() if v is not None]

    @property
    def missing_keys(self) -> list[str]:
        return [k for k, v in self.tables.items() if v is None]

    def summary_frame(self) -> pd.DataFrame:
        """파일별 로딩 요약 (보고서/콘솔용)."""
        rows = []
        for key in schema.TABLE_KEYS:
            info = self.load_report[key]
            rows.append({
                "key": key,
                "status": info.status,
                "file": Path(info.path).name if info.path else "",
                "match": info.match_method or "",
                "rows": info.rows,
                "cols": info.n_columns,
                "missing_cols": ", ".join(info.missing_columns),
                "extra_cols": ", ".join(info.extra_columns),
                "renamed": ", ".join(f"{a}→{b}" for a, b in info.renamed_columns.items()),
                "encoding": info.encoding or "",
                "list_delims": ", ".join(f"{c}:{d}" for c, d in info.list_delimiters.items()),
                "warnings": len(info.parse_warnings) + (1 if info.n_bad_lines else 0),
                "memory_mb": round(info.memory_mb, 2),
            })
        return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# 1. 데이터 폴더 준비 (zip 해제)
# ---------------------------------------------------------------------------

def _zip_member_name(info: zipfile.ZipInfo) -> str:
    """zip 안 파일명 복원. UTF-8 플래그가 없으면 cp437 → utf-8/cp949 재해석 (윈도우 한글 zip 대응)."""
    name = info.filename
    if info.flag_bits & 0x800:
        return name
    try:
        raw = name.encode("cp437")
    except UnicodeEncodeError:
        return name
    for enc in ("utf-8", "cp949"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return name


def extract_zip(zip_path: Path, extract_root: Path) -> Path:
    """zip 을 extract_root/<zip 이름>/ 에 푼다 (이미 있으면 지우고 새로). 푼 폴더를 돌려준다."""
    extract_root.mkdir(parents=True, exist_ok=True)
    safe_stem = re.sub(r"[^0-9A-Za-z가-힣_\-.]+", "_", zip_path.stem) or "data"
    dest = (extract_root / safe_stem).resolve()
    if extract_root.resolve() not in dest.parents:
        raise ValueError(f"압축 해제 위치가 이상함: {dest}")
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            name = _zip_member_name(info).replace("\\", "/")
            if info.is_dir() or name.endswith("/"):
                continue
            parts = [p for p in name.split("/") if p not in ("", ".")]
            if not parts or ".." in parts or parts[0] == "__MACOSX" or parts[-1].startswith("._"):
                continue
            target = dest.joinpath(*parts).resolve()
            if dest not in target.parents:
                continue  # zip-slip 방지
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)
    return dest


def prepare_data_dir(path: PathLike, extract_root: Optional[PathLike] = None) -> tuple[Path, list[str]]:
    """경로가 zip 이면 풀고, 폴더에 CSV 없이 zip 만 있으면 그 zip 들을 푼다. (읽을 폴더, 메모) 반환."""
    p = Path(path)
    root = Path(extract_root) if extract_root else DEFAULT_EXTRACT_ROOT
    notes: list[str] = []
    if p.is_file() and zipfile.is_zipfile(p):
        dest = extract_zip(p, root)
        notes.append(f"zip 해제: {p} → {dest}")
        return dest, notes
    if p.is_dir():
        if find_csv_files(p):
            return p, notes
        zips = [z for z in sorted(p.rglob("*.zip")) if len(z.relative_to(p).parts) <= 2]
        if zips:
            batch_root = root / (re.sub(r"[^0-9A-Za-z가-힣_\-.]+", "_", p.name) or "data")
            for z in zips:
                dest = extract_zip(z, batch_root)
                notes.append(f"zip 해제: {z} → {dest}")
            return batch_root, notes
        return p, notes
    if not p.exists():
        notes.append(f"데이터 경로가 없음: {p}")
    return p, notes


# ---------------------------------------------------------------------------
# 2. 파일 찾기
# ---------------------------------------------------------------------------

def find_csv_files(root: Path, max_depth: int = MAX_SEARCH_DEPTH) -> list[Path]:
    """root 아래 CSV 목록 (깊이 max_depth 까지, 숨김/__MACOSX/임시 파일 제외). 얕은 것부터 정렬."""
    if not root.is_dir():
        return []
    out: list[Path] = []
    for p in root.rglob("*"):
        if not p.is_file() or p.suffix.lower() not in CSV_SUFFIXES:
            continue
        rel = p.relative_to(root)
        if len(rel.parts) - 1 > max_depth:
            continue
        if any(part.startswith(("__MACOSX", ".")) for part in rel.parts):
            continue
        if p.name.startswith(("._", "~$")):
            continue
        out.append(p)
    return sorted(out, key=lambda q: (len(q.relative_to(root).parts), q.name, str(q)))


def _stem_core(path: Path) -> str:
    """'09_sales (1).csv' → 'sales'. 앞 번호와 괄호 꼬리표를 떼고 정규화."""
    stem = re.sub(r"\(\d+\)$", "", path.stem).strip()
    stem = re.sub(r"^\d+[\s_\-.]*", "", stem)
    return normalize_colname(stem)


def _name_score(path: Path, spec: schema.TableSpec) -> float:
    """파일명(번호 뗀 부분)과 테이블 이름/힌트의 유사도 0~1.

    같으면 1.0, 힌트 단어가 파일명 단어에 모두 들어 있으면 0.8~1.0 (파일명 단어 중 차지 비율이 클수록 높음),
    아니면 문자열 유사도(difflib).
    """
    core = _stem_core(path)
    if not core:
        return 0.0
    core_tokens = set(core.split("_"))
    targets = [spec.short_name] + [normalize_colname(h) for h in spec.name_hints]
    best = 0.0
    for t in targets:
        if not t:
            continue
        if core == t:
            return 1.0
        t_tokens = set(t.split("_"))
        if t_tokens <= core_tokens:
            best = max(best, 0.8 + 0.2 * len(t_tokens) / len(core_tokens))
        best = max(best, difflib.SequenceMatcher(None, core, t).ratio())
    return best


def _first_line(path: Path) -> tuple[str, Optional[str]]:
    """첫 줄을 문자열로 (가능한 인코딩으로). (줄, 인코딩) 반환."""
    with open(path, "rb") as f:
        head = f.read(65536)
    line = head.split(b"\n", 1)[0].rstrip(b"\r")
    for enc in ENCODINGS:
        try:
            return line.decode(enc).lstrip("﻿"), enc
        except UnicodeDecodeError:
            continue
    return line.decode("utf-8", errors="replace").lstrip("﻿"), None


def _count_outside_quotes(line: str, ch: str) -> int:
    n, in_q = 0, False
    for c in line:
        if c == '"':
            in_q = not in_q
        elif c == ch and not in_q:
            n += 1
    return n


def detect_csv_sep(header_line: str) -> str:
    """헤더 줄에서 가장 많이 나오는 구분자 (, 탭 ; |). 없으면 ','."""
    counts = {sep: _count_outside_quotes(header_line, sep) for sep in (",", "\t", ";", "|")}
    best = max(counts, key=lambda s: counts[s])
    return best if counts[best] > 0 else ","


def _header_names(path: Path) -> list[str]:
    try:
        line, _ = _first_line(path)
        sep = detect_csv_sep(line)
        return next(csv.reader([line], delimiter=sep))
    except Exception:  # 헤더를 못 읽는 파일은 매칭 대상에서 빠질 뿐이다
        return []


@dataclass
class _Resolution:
    path: Path
    method: str
    others: list[str]


def resolve_files(csv_paths: list[Path], root: Path) -> tuple[dict[str, _Resolution], list[Path]]:
    """CSV 경로들을 17개 파일 키에 배정한다. (배정 결과, 배정 안 된 파일) 반환."""
    result: dict[str, _Resolution] = {}
    used: set[Path] = set()

    # 1순위: 앞 번호
    for spec in schema.TABLES:
        pat = re.compile(rf"^0*{spec.number}(?!\d)")
        cands = [p for p in csv_paths if pat.match(p.name) and p not in used]
        if not cands:
            continue
        cands.sort(key=lambda p: (-_name_score(p, spec), len(p.relative_to(root).parts), len(p.name), str(p)))
        best = cands[0]
        result[spec.key] = _Resolution(best, "number_prefix", [str(c) for c in cands[1:]])
        used.add(best)

    # 2순위: 헤더 컬럼 겹침
    leftovers = [p for p in csv_paths if p not in used]
    unresolved = [s for s in schema.TABLES if s.key not in result]
    if leftovers and unresolved:
        headers = {p: {normalize_colname(c) for c in _header_names(p)} for p in leftovers}
        scores = []
        for spec in unresolved:
            expected = set(spec.column_names)
            for p, got in headers.items():
                if got:
                    scores.append((len(expected & got) / len(expected | got), spec.key, p))
        scores.sort(key=lambda t: (-t[0], t[1], str(t[2])))
        for jac, key, p in scores:
            if jac < HEADER_OVERLAP_CUTOFF:
                break
            if key in result or p in used:
                continue
            result[key] = _Resolution(p, f"header_overlap({jac:.2f})", [])
            used.add(p)

    # 3순위: 파일명 유사도 (전체 조합 중 점수 높은 것부터 1:1 배정)
    leftovers = [p for p in csv_paths if p not in used]
    pairs = sorted(((_name_score(p, spec), spec.key, p) for spec in schema.TABLES if spec.key not in result
                    for p in leftovers), key=lambda t: (-t[0], t[1], str(t[2])))
    for score, key, p in pairs:
        if score < NAME_SIMILARITY_CUTOFF:
            break
        if key in result or p in used:
            continue
        result[key] = _Resolution(p, f"name_similarity({score:.2f})", [])
        used.add(p)

    unmatched = [p for p in csv_paths if p not in used]
    return result, unmatched


# ---------------------------------------------------------------------------
# 3. CSV 읽기
# ---------------------------------------------------------------------------

def map_columns(actual: list[str], spec: schema.TableSpec) -> tuple[dict[str, str], dict[str, str]]:
    """파일의 실제 컬럼명 → 명세 컬럼명 매핑. (전체 매핑, 이름이 바뀐 것만) 반환."""
    expected = spec.column_names
    mapping: dict[str, str] = {}
    renamed: dict[str, str] = {}
    used: set[str] = set()
    norm = {a: normalize_colname(a) for a in actual}
    aliases = {normalize_colname(k): v for k, v in schema.COLUMN_ALIASES.get(spec.key, {}).items()}
    for a in actual:
        n = norm[a]
        target = n if n in expected else aliases.get(n)
        if target and target not in used:
            mapping[a] = target
            used.add(target)
            if a != target:
                renamed[a] = target
    missing = [e for e in expected if e not in used]
    leftovers = [a for a in actual if a not in mapping]
    scored = sorted(
        ((difflib.SequenceMatcher(None, norm[a], e).ratio(), a, e) for a in leftovers for e in missing),
        key=lambda t: (-t[0], t[1], t[2]),
    )
    for ratio, a, e in scored:
        if ratio < FUZZY_COLUMN_CUTOFF:
            break
        if a in mapping or e in used:
            continue
        mapping[a] = e
        renamed[a] = e
        used.add(e)
    return mapping, renamed


def _dtype_for(kind: Optional[str]) -> Optional[type]:
    """숫자 kind 는 pandas 추론에 맡기고(빠르고 메모리 적음), 나머지는 문자열로 읽는다."""
    if kind is None or kind in schema.NUMERIC_KINDS:
        return None
    return str


def repair_overflow_fields(fields: list[str], kinds: list[Optional[str]]) -> Optional[list[str]]:
    """칸 수가 헤더보다 많은 줄을 복구한다 (목록 컬럼을 따옴표 없이 쉼표로 쓴 경우). 실패하면 None.

    - 목록 컬럼이 1개: 넘친 칸을 그 컬럼에 합친다.
    - 목록 컬럼이 여러 개이고 모두 요일 목록: 요일처럼 보이는 연속 토큰을 각 목록 컬럼에 배정한다.
    """
    n = len(kinds)
    extra = len(fields) - n
    if extra <= 0:
        return None
    list_pos = [i for i, k in enumerate(kinds) if k in ("list", "weekday_list")]
    if len(list_pos) == 1:
        p = list_pos[0]
        return fields[:p] + [",".join(fields[p:p + extra + 1])] + fields[p + extra + 1:]
    if list_pos and all(kinds[p] == "weekday_list" for p in list_pos):
        out: list[str] = []
        i, remaining = 0, extra
        for kind in kinds:
            if i >= len(fields):
                return None
            if kind == "weekday_list":
                take = [fields[i]]
                i += 1
                while remaining > 0 and i < len(fields) and expand_weekday_token(fields[i]) is not None:
                    take.append(fields[i])
                    i += 1
                    remaining -= 1
                out.append(",".join(take))
            else:
                out.append(fields[i])
                i += 1
        if remaining == 0 and i == len(fields):
            return out
    return None


def _read_rows_manually(path: Path, sep: str, enc: str, header: list[str], kinds: list[Optional[str]],
                        info: TableLoadInfo, encoding_errors: str = "strict") -> pd.DataFrame:
    """칸 수가 맞지 않는 줄이 있을 때의 느린 경로: csv 모듈로 한 줄씩 읽어 복구/채움/기록한다.

    - 칸이 많은 줄: repair_overflow_fields 로 목록 컬럼에 합쳐 복구, 실패하면 bad_lines 에 기록(제외)
    - 칸이 적은 줄: 빈 값으로 채우고 개수를 경고로 남긴다
    모든 값은 문자열로 읽으며 숫자 변환은 coerce_table 이 한다.
    """
    n = len(header)
    rows: list[list[Optional[str]]] = []
    bad: list[str] = []
    n_repaired = n_padded = 0
    with open(path, "r", encoding=enc, errors=encoding_errors, newline="") as f:
        reader = csv.reader(f, delimiter=sep)
        next(reader, None)  # 헤더
        for line_no, fields in enumerate(reader, start=2):
            if len(fields) == n:
                rows.append(fields)
            elif len(fields) > n:
                fixed = repair_overflow_fields(fields, kinds)
                if fixed is None:
                    bad.append(f"{line_no}행: " + sep.join(fields))
                else:
                    rows.append(fixed)
                    n_repaired += 1
            elif any(x.strip() for x in fields):
                rows.append(fields + [""] * (n - len(fields)))
                n_padded += 1
    df = pd.DataFrame(rows, columns=header, dtype=object).replace({"": None})
    info.n_repaired_lines, info.n_bad_lines, info.bad_lines = n_repaired, len(bad), bad[:MAX_KEPT_BAD_LINES]
    if n_repaired:
        info.parse_warnings.append(f"목록 컬럼을 따옴표 없이 쓴 것으로 보이는 줄 {n_repaired}개를 복구함")
    if n_padded:
        info.parse_warnings.append(f"칸이 모자란 줄 {n_padded}개는 빈 값으로 채움")
    if bad:
        info.parse_warnings.append(f"복구하지 못한 줄 {len(bad)}개는 제외함 (load_report.md 의 '복구 못 한 줄' 참고)")
    return df


def _read_body(path: Path, sep: str, enc: str, header: list[str], dtype: dict[str, Any],
               kinds: list[Optional[str]], info: TableLoadInfo, encoding_errors: str = "strict") -> pd.DataFrame:
    """빠른 경로(pandas C 파서). 칸 수 오류가 나면 _read_rows_manually 로 넘어간다."""
    try:
        return pd.read_csv(path, sep=sep, encoding=enc, dtype=dtype, index_col=False, low_memory=False,
                           encoding_errors=encoding_errors)
    except pd.errors.ParserError as e:
        info.parse_warnings.append(f"칸 수가 맞지 않는 줄 발견 → 한 줄씩 다시 읽음 ({str(e).splitlines()[0][:120]})")
    return _read_rows_manually(path, sep, enc, header, kinds, info, encoding_errors)


def read_csv_robust(path: Path, spec: schema.TableSpec, info: TableLoadInfo) -> pd.DataFrame:
    """인코딩/구분자/컬럼명 차이를 견디며 CSV 를 읽고 컬럼명을 명세 이름으로 바꿔 돌려준다."""
    with open(path, "rb") as f:
        head = f.read(65536)
    if not head.strip():
        raise ValueError("빈 파일 (0바이트 또는 공백뿐)")
    has_bom = head.startswith(codecs.BOM_UTF8)
    first = head.split(b"\n", 1)[0].rstrip(b"\r")
    attempts: list[tuple[str, str]] = [(e, "strict") for e in ENCODINGS] + [("utf-8", "replace")]
    last_error: Optional[Exception] = None
    for enc, errors in attempts:
        try:
            line = first.decode(enc, errors=errors).lstrip("﻿")
            sep = detect_csv_sep(line)
            header = list(pd.read_csv(path, sep=sep, encoding=enc, nrows=0, index_col=False,
                                      encoding_errors=errors).columns)
            mapping, renamed = map_columns(header, spec)
            kinds = [spec.column(mapping[h]).kind if h in mapping else None for h in header]
            dtype = {h: _dtype_for(k) for h, k in zip(header, kinds) if _dtype_for(k) is not None}
            df = _read_body(path, sep, enc, header, dtype, kinds, info, encoding_errors=errors)
        except UnicodeDecodeError as e:
            last_error = e
            continue
        info.encoding = "utf-8-sig" if (has_bom and enc == "utf-8") else enc
        if errors == "replace":
            info.encoding = "utf-8(깨진 문자 대체)"
            info.parse_warnings.append("utf-8/utf-8-sig/cp949 모두 실패 → 깨진 문자를 대체하고 읽음")
        info.csv_sep = {",": "comma", "\t": "tab", ";": "semicolon", "|": "pipe"}.get(sep, sep)
        info.columns_in_file = [str(h) for h in header]
        info.renamed_columns = {str(a): b for a, b in renamed.items()}
        df = df.rename(columns={a: b for a, b in mapping.items() if a != b})
        df.columns = [str(c).replace("﻿", "").strip() for c in df.columns]
        return df
    raise ValueError(f"인코딩 판별 실패: {last_error}")


# ---------------------------------------------------------------------------
# 4. 형 변환
# ---------------------------------------------------------------------------

def _coerce_column(df: pd.DataFrame, col: schema.ColumnSpec, info: TableLoadInfo) -> None:
    """df(로더가 막 만든 프레임)의 컬럼 하나를 kind 에 맞게 바꾼다."""
    s = df[col.name]
    kind = col.kind
    issues: list[str] = []
    if kind == "id":
        df[col.name], issues = clean_id(s)
    elif kind in ("str", "category", "weekday"):
        df[col.name], issues = clean_text(s)
    elif kind == "date":
        df[col.name], issues = coerce_date(s)
    elif kind == "time_hhmm":
        minutes, issues = coerce_hhmm(s)
        df[col.name] = clean_text(s)[0]
        df[f"{col.name}_min"] = minutes
    elif kind == "hour":
        df[col.name], issues = coerce_hour(s)
    elif kind == "int":
        df[col.name], issues = coerce_numeric(s, integer=True)
    elif kind == "float":
        df[col.name], issues = coerce_numeric(s, integer=False)
    elif kind == "flag01":
        df[col.name], issues = coerce_flag01(s)
    elif kind == "yn":
        df[col.name], issues = coerce_yn(s)
    elif kind in ("list", "weekday_list"):
        raw = clean_text(s)[0]
        delim = detect_list_delimiter(raw)
        info.list_delimiters[col.name] = delimiter_name(delim)
        df[f"{col.name}_raw"] = raw
        df[col.name] = raw.map(lambda v: parse_list_cell(v, delim))
    info.parse_warnings.extend(f"{col.name}: {m}" for m in issues)


def coerce_table(df: pd.DataFrame, spec: schema.TableSpec, info: TableLoadInfo) -> pd.DataFrame:
    """명세에 있는 컬럼만 kind 에 맞게 변환한다 (없는 컬럼은 건너뜀). 새 프레임을 돌려준다."""
    out = df.copy()
    for col in spec.columns:
        if col.name in out.columns:
            _coerce_column(out, col, info)
    return out


def _load_one(path: Path, spec: schema.TableSpec, info: TableLoadInfo) -> pd.DataFrame:
    t0 = time.time()
    raw = read_csv_robust(path, spec, info)
    expected = spec.column_names
    info.missing_columns = [c for c in expected if c not in raw.columns]
    info.extra_columns = [c for c in raw.columns if c not in expected]
    df = coerce_table(raw, spec, info)
    info.rows = int(len(df))
    info.n_columns = int(raw.shape[1])
    if info.rows == 0:
        info.parse_warnings.append("데이터 행이 없음 (헤더만 있음)")
    if info.missing_columns:
        info.parse_warnings.append(f"명세 컬럼 누락: {', '.join(info.missing_columns)}")
    info.memory_mb = float(df.memory_usage(deep=True).sum()) / 1e6
    info.seconds = round(time.time() - t0, 2)
    info.status = "ok"
    return df


# ---------------------------------------------------------------------------
# 5. 요일 정규화 (공급사 order_days/delivery_days, 달력 weekday)
# ---------------------------------------------------------------------------

def _decide_numeric_scheme(sup: Optional[pd.DataFrame], cal: Optional[pd.DataFrame]) -> dict[str, Any]:
    """숫자 요일 표기가 있으면 체계를 정한다. 달력이 숫자면 실제 날짜와 대조해 확정."""
    tokens: list[str] = []
    if sup is not None:
        for col in ("order_days", "delivery_days"):
            if col in sup.columns:
                tokens += [str(t) for lst in sup[col] for t in lst]
    cal_tokens: list[str] = []
    if cal is not None and "weekday" in cal.columns:
        cal_tokens = [str(t) for t in cal["weekday"].dropna()]
    decision: dict[str, Any] = {"numeric_scheme": None, "source": "문자 요일 표기 (숫자 아님)", "note": ""}
    if not tokens and not cal_tokens:
        decision["source"] = "요일 데이터 없음 (05_suppliers / 07_calendar 미로드)"
        return decision
    if not any(is_numeric_weekday_token(t) for t in tokens + cal_tokens):
        return decision
    cal_numeric = bool(cal_tokens) and all(is_numeric_weekday_token(t) for t in cal_tokens)
    if cal_numeric and cal is not None and "date" in cal.columns:
        scheme, rate = infer_numeric_scheme_from_dates(cal["date"], cal["weekday"])
        decision.update(numeric_scheme=scheme, source=f"달력 date 와 대조해 확정 (일치율 {rate:.1%})",
                        note=NUMERIC_SCHEME_DESC[scheme])
    else:
        scheme, note = detect_numeric_weekday_scheme(tokens + cal_tokens)
        decision.update(numeric_scheme=scheme, source="숫자 범위로 추정 (달력이 숫자가 아님)",
                        note=f"{NUMERIC_SCHEME_DESC[scheme]} — {note}")
    return decision


def _normalize_supplier_days(sup: pd.DataFrame, scheme: Optional[str], info: TableLoadInfo) -> pd.DataFrame:
    out = sup.copy()
    for col in ("order_days", "delivery_days"):
        if col not in out.columns:
            continue
        results = out[col].map(lambda toks: normalize_weekdays_verbose(toks, scheme))
        out[col] = results.map(lambda r: r[0])
        unknown = sorted({u for r in results for u in r[1]})
        distinct = sorted({str(t) for lst in sup[col] for t in lst})
        token_map = {}
        for t in distinct:
            exp = expand_weekday_token(t, scheme)
            token_map[t] = f"{exp}={weekday_names(exp)}" if exp else None
        info.weekday_mapping[col] = {"numeric_scheme": scheme, "token_map": token_map, "unknown_tokens": unknown}
        if unknown:
            info.parse_warnings.append(f"{col}: 해석 못 한 요일 토큰 {unknown} (무시됨)")
        n_empty = int((out[col].map(len) == 0).sum())
        if n_empty:
            info.parse_warnings.append(f"{col}: 요일이 비어 있는 공급사 {n_empty}곳 (원본 {col}_raw 확인)")
    return out


def _single_iso(value: Any, scheme: Optional[str]) -> float:
    days = normalize_weekdays([value], scheme) if value is not None else []
    return float(days[0]) if len(days) == 1 else np.nan


def _normalize_calendar_weekday(cal: pd.DataFrame, scheme: Optional[str], info: TableLoadInfo) -> pd.DataFrame:
    out = cal.copy()
    iso = out["weekday"].map(lambda v: _single_iso(v, scheme)).astype("float64")
    out["weekday_iso"] = iso.astype("int64") if iso.notna().all() and len(iso) else iso
    info.weekday_mapping["weekday"] = {
        "numeric_scheme": scheme,
        "token_map": {str(t): weekday_names(normalize_weekdays([t], scheme)) or None
                      for t in sorted(out["weekday"].dropna().astype(str).unique())[:20]},
    }
    n_bad = int(iso.isna().sum() - out["weekday"].isna().sum())
    if n_bad > 0:
        info.parse_warnings.append(f"weekday: 요일로 해석 못 한 값 {n_bad}건")
    if "date" in out.columns and pd.api.types.is_datetime64_any_dtype(out["date"]):
        actual = out["date"].dt.isocalendar().day.astype("float64")
        mismatch = iso.notna() & actual.notna() & (iso != actual)
        if mismatch.any():
            info.parse_warnings.append(f"weekday: 실제 날짜의 요일과 다른 행 {int(mismatch.sum())}건")
    return out


def normalize_weekday_columns(tables: dict[str, Optional[pd.DataFrame]], reports: dict[str, TableLoadInfo],
                              forced_scheme: Optional[str] = None) -> dict[str, Any]:
    """공급사·달력 요일 컬럼을 ISO 정수로 바꾼다 (tables 를 새 프레임으로 교체). 판단 결과를 돌려준다.

    forced_scheme: 숫자 요일 체계를 직접 지정 ('mon0' | 'sun0' | 'iso' | 'sun1'). 자동 판단이 틀렸을 때만 쓴다.
    """
    sup, cal = tables.get("05_suppliers"), tables.get("07_calendar")
    if forced_scheme:
        if forced_scheme not in NUMERIC_SCHEMES:
            raise ValueError(f"weekday_scheme 은 {list(NUMERIC_SCHEMES)} 중 하나: {forced_scheme}")
        decision = {"numeric_scheme": forced_scheme, "source": "사용자 지정 (weekday_scheme 옵션)",
                    "note": NUMERIC_SCHEME_DESC[forced_scheme]}
    else:
        decision = _decide_numeric_scheme(sup, cal)
    scheme = decision["numeric_scheme"]
    if sup is not None:
        tables["05_suppliers"] = _normalize_supplier_days(sup, scheme, reports["05_suppliers"])
        reports["05_suppliers"].weekday_mapping["_scheme"] = decision
    if cal is not None and "weekday" in cal.columns:
        tables["07_calendar"] = _normalize_calendar_weekday(cal, scheme, reports["07_calendar"])
        reports["07_calendar"].weekday_mapping["_scheme"] = decision
    return decision


# ---------------------------------------------------------------------------
# 6. 진입점
# ---------------------------------------------------------------------------

def load_all(data_dir: PathLike, strict: bool = False, extract_root: Optional[PathLike] = None,
             weekday_scheme: Optional[str] = None) -> Dataset:
    """데이터 폴더(또는 zip)를 읽어 Dataset 을 만든다.

    strict=False(기본): 파일이 없거나 읽다가 실패해도 경고만 남기고 해당 키를 None 으로 둔다.
    strict=True       : 누락/실패 파일이 하나라도 있으면 FileNotFoundError / ValueError.
    extract_root      : zip 을 풀 폴더 (기본 data/_extracted)
    weekday_scheme    : 숫자 요일 체계 강제 지정 ('mon0' 0=월, 'sun0' 0=일, 'iso' 1=월, 'sun1' 1=일). 기본 자동.
    """
    t0 = time.time()
    root, notes = prepare_data_dir(data_dir, extract_root)
    warnings = list(notes)
    csvs = find_csv_files(root)
    if root.exists() and not csvs:
        warnings.append(f"CSV 파일이 하나도 없음: {root}")
    resolution, unmatched = resolve_files(csvs, root) if csvs else ({}, [])

    tables: dict[str, Optional[pd.DataFrame]] = {}
    reports: dict[str, TableLoadInfo] = {}
    for spec in schema.TABLES:
        info = TableLoadInfo(key=spec.key, expected_filename=spec.filename)
        reports[spec.key] = info
        res = resolution.get(spec.key)
        if res is None:
            tables[spec.key] = None
            warnings.append(f"[누락] {spec.filename} 를 찾지 못함 → 이 테이블이 필요한 검사는 SKIP")
            continue
        info.path, info.match_method, info.other_candidates = str(res.path), res.method, res.others
        if res.others:
            warnings.append(f"[중복 후보] {spec.key}: {res.path.name} 를 사용, 나머지 {len(res.others)}개 무시")
        if res.method != "number_prefix":
            warnings.append(f"[이름 다름] {spec.key} ← {res.path.name} ({res.method})")
        try:
            tables[spec.key] = _load_one(res.path, spec, info)
            if info.missing_columns:
                warnings.append(f"[컬럼 누락] {spec.key}: {', '.join(info.missing_columns)} → 이 컬럼이 필요한 검사는 SKIP")
            if info.renamed_columns:
                warnings.append(f"[컬럼명 보정] {spec.key}: " + ", ".join(f"{a}→{b}" for a, b in info.renamed_columns.items()))
            if info.n_bad_lines:
                warnings.append(f"[복구 못 한 줄] {spec.key}: {info.n_bad_lines}줄 제외 (load_report.md 참고)")
        except Exception as e:  # 한 파일이 깨져도 나머지는 계속 읽는다
            tables[spec.key] = None
            info.status = "error"
            info.error = f"{type(e).__name__}: {e}"
            warnings.append(f"[읽기 실패] {spec.key} ({res.path.name}): {info.error}")

    decision = normalize_weekday_columns(tables, reports, weekday_scheme)
    for p in unmatched:
        warnings.append(f"[알 수 없는 CSV] {p.name} — 17개 파일 어디에도 배정되지 않음")

    if strict:
        missing = [k for k in schema.TABLE_KEYS if reports[k].status == "missing"]
        failed = [k for k in schema.TABLE_KEYS if reports[k].status == "error"]
        if missing:
            raise FileNotFoundError(f"누락 파일: {', '.join(missing)} (폴더: {root})")
        if failed:
            raise ValueError(f"읽기 실패 파일: {', '.join(failed)}")

    return Dataset(
        tables=tables,
        load_report=reports,
        data_dir=root,
        source=str(data_dir),
        warnings=warnings,
        unmatched_files=[str(p) for p in unmatched],
        meta={"weekday_scheme": decision, "n_csv_found": len(csvs), "load_seconds": round(time.time() - t0, 2)},
    )


def iter_loaded(ds: Dataset) -> Iterator[tuple[str, pd.DataFrame]]:
    """(키, DataFrame) 을 로드된 것만 순서대로."""
    for key in schema.TABLE_KEYS:
        df = ds.tables.get(key)
        if df is not None:
            yield key, df


class MissingDataError(KeyError):
    """필요한 테이블/컬럼이 없을 때 prep/validate 가 던지는 오류."""

    def __str__(self) -> str:  # KeyError 의 따옴표 출력 대신 메시지 그대로
        return str(self.args[0]) if self.args else "필요한 데이터 없음"


def require(ds: Dataset, key: str, columns: tuple[str, ...] | list[str] = ()) -> pd.DataFrame:
    """테이블과 컬럼이 있으면 DataFrame 을, 없으면 MissingDataError(한국어 메시지)를 던진다."""
    df = ds.get(key)
    if df is None:
        raise MissingDataError(f"{key} 테이블이 없음 (load_report 확인)")
    miss = [c for c in columns if c not in df.columns]
    if miss:
        raise MissingDataError(f"{key} 에 컬럼 없음: {', '.join(miss)} (load_report 확인)")
    return df
