"""
엔진 파라미터 (사용자가 UI 에서 바꾸는 값 + 데이터에 없어서 정한 가정값).

- 모든 엔진 함수는 EngineParams 하나를 받는다. 기준일이 비어 있으면 resolve_params(ds, params) 가 데이터로 채운다.
- '가정' 표시가 필요한 값(데이터에서 나온 값이 아님)은 ASSUMPTION_PARAMS 에 이름과 설명을 둔다. UI 가 이 표로 배지를 단다.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, replace
from typing import Any, Optional

import pandas as pd

from ..loader import Dataset
from .. import prep

DEFAULT_OVERSTOCK_COVER_DAYS = 7.0
DEFAULT_LOW_STOCK_COVER_DAYS = 1.5


@dataclass(frozen=True)
class EngineParams:
    """엔진 한 번 실행에 쓰는 조건.

    asof_date            : 기준일 D (D 영업 종료 후 재고에서 출발). None 이면 10_inventory_daily 마지막 날
    decision_time        : 발주 판단 시각 (D+1 의 HH:MM). 공급사 마감 판정에 쓴다
    horizon_days         : 예측·재고 전개 기간 (D+1 … D+H)
    lookback_weeks       : 같은 요일 값을 몇 회 평균할지 (기본 4회)
    max_lookback_weeks   : 공휴일·미취급일을 건너뛰며 같은 요일 값을 모을 때 최대 몇 주 거슬러 올라갈지 (기본 6)
    apply_trend          : 추세 계수(최근 2주/8주) 적용 여부. 실데이터 백테스트(2026-10-31)에서 추세 보정이
                           모든 지표를 악화시켜 기본값은 False
    service_level        : 안전재고 서비스 수준 (z 값으로 환산)
    sigma_method         : 안전재고 σ 산출 방식. "backtest" = 같은 예측식을 과거에 매일 적용한 식자재 단위 잔차 RMSE
                           (커버일수별), "menu_sum" = 메뉴 σ × BOM 단순 합(보수적, 이전 방식)
    shift_band           : 급변 판정 밴드. 식자재 소요량 최근 2주 평균 / 8주 평균이 밴드 밖이면 급변
    shift_sigma_mult     : 급변 품목의 σ 배율 (백테스트: 급변 기준일 오차 ≈ 평시의 1.6배)
    demand_multiplier    : 전체 수요 시나리오 배율 (1.1 = +10%)
    menu_multipliers     : 메뉴별 추가 배율 {menu_id: 배율}
    apply_promo          : 예측 기간 프로모션 계수 적용 여부 (대상 메뉴만)
    apply_holiday        : 공휴일 계수 적용 여부 (매장별, 평일 공휴일/주말 공휴일 구분)
    delay_buffer_days    : 입고 예정 PO 도착을 며칠 늦게 볼지 (입고 지연 보정)
    expiry_alert_days    : 사용기한 임박 알림 기준 일수
    overstock_cover_days : 과잉 알림 기준 커버일수. None 이면 제공 플래그 역산값(없으면 7)
    emergency_markup     : 긴급발주 단가 할증률 (가정)
    transfer_cost        : 매장 간 이동 1건 비용 (원, 가정)
    """

    asof_date: Optional[pd.Timestamp] = None
    decision_time: str = "09:00"
    horizon_days: int = 14
    lookback_weeks: int = 4
    max_lookback_weeks: int = 6
    apply_trend: bool = False
    service_level: float = 0.95
    sigma_method: str = "backtest"
    shift_band: tuple[float, float] = (0.77, 1.3)
    shift_sigma_mult: float = 1.6
    demand_multiplier: float = 1.0
    menu_multipliers: dict[str, float] = field(default_factory=dict)
    apply_promo: bool = True
    apply_holiday: bool = True
    delay_buffer_days: int = 0
    expiry_alert_days: int = 3
    overstock_cover_days: Optional[float] = None
    emergency_markup: float = 0.2
    transfer_cost: float = 0.0

    @property
    def asof(self) -> pd.Timestamp:
        """기준일 (resolve_params 를 거친 뒤에만 쓴다)."""
        if self.asof_date is None:
            raise ValueError("asof_date 가 비어 있음 → resolve_params(ds, params) 를 먼저 호출")
        return pd.Timestamp(self.asof_date).normalize()

    @property
    def decision_datetime(self) -> pd.Timestamp:
        """발주 판단 시각 = 기준일 다음 날 + decision_time."""
        hh, mm = (int(x) for x in str(self.decision_time).split(":")[:2])
        return self.asof + pd.Timedelta(days=1, hours=hh, minutes=mm)

    @property
    def horizon_dates(self) -> pd.DatetimeIndex:
        """예측 날짜 D+1 … D+H."""
        return pd.date_range(self.asof + pd.Timedelta(days=1), periods=int(self.horizon_days), freq="D")

    def cache_key(self) -> str:
        """캐시 키용 문자열 (dict 포함 값을 정렬해 JSON 으로)."""
        d = asdict(self)
        d["asof_date"] = None if self.asof_date is None else str(pd.Timestamp(self.asof_date).date())
        d["menu_multipliers"] = dict(sorted((self.menu_multipliers or {}).items()))
        d["shift_band"] = list(self.shift_band)
        return json.dumps(d, ensure_ascii=False, sort_keys=True, default=str)


# 데이터가 아니라 가정에서 나온 파라미터 (UI 배지·가정 목록용)
ASSUMPTION_PARAMS: dict[str, str] = {
    "service_level": "안전재고 서비스 수준 (사용자 선택값)",
    "demand_multiplier": "수요 시나리오 배율 (사용자 입력)",
    "delay_buffer_days": "입고 지연 보정 일수 (사용자 입력)",
    "emergency_markup": "긴급발주 할증률 (실데이터: 긴급 단가 = 정기 단가, 가정값)",
    "transfer_cost": "매장 간 이동 비용 (데이터에 없음, 가정값)",
    "expiry_alert_days": "사용기한 임박 기준 일수 (가정값)",
    "overstock_cover_days": "과잉 기준 커버일수 (제공 플래그 역산 또는 가정값)",
    "shift_sigma_mult": "급변 품목 σ 배율 (백테스트 급변/평시 오차 비율에서 정한 값)",
    "shift_band": "급변 판정 밴드 (최근 2주/8주 소요 비율, 가정값)",
}


def flag_thresholds(ds: Dataset) -> dict[str, Any]:
    """제공 데이터의 low_stock_flag / overstock_flag 를 커버일수 임계값으로 역산 (validate 검사 재사용).

    반환: {'low_stock_flag': {'threshold', 'accuracy'}, 'overstock_flag': {...}} — 계산 불가면 빈 dict.
    """
    try:
        from ..checks_flow import check_flag_thresholds
        res = check_flag_thresholds(ds)
        return dict(res[0].metrics) if res else {}
    except Exception:  # 검사 코드가 실데이터에서 깨져도 엔진은 기본값으로 계속
        return {}


def resolve_params(ds: Dataset, params: EngineParams) -> tuple[EngineParams, dict[str, Any]]:
    """비어 있는 값을 데이터로 채운 새 EngineParams 와 근거 메모를 돌려준다.

    - asof_date: 10_inventory_daily 마지막 날 (prep.default_asof)
    - overstock_cover_days: 제공 overstock_flag 역산 임계값 (정확도 ≥ 95% 일 때), 아니면 7일
    """
    notes: dict[str, Any] = {}
    asof = params.asof_date
    if asof is None:
        asof = prep.default_asof(ds)
        notes["asof_source"] = "10_inventory_daily 마지막 날"
    else:
        notes["asof_source"] = "사용자 선택"
    thresholds = flag_thresholds(ds)
    notes["flag_thresholds"] = thresholds
    over = params.overstock_cover_days
    if over is None:
        m = thresholds.get("overstock_flag") or {}
        if m.get("threshold") is not None and m.get("accuracy", 0) >= 0.95:
            over = float(m["threshold"])
            notes["overstock_source"] = f"제공 overstock_flag 역산 (정확도 {m['accuracy']:.1%})"
        else:
            over = DEFAULT_OVERSTOCK_COVER_DAYS
            notes["overstock_source"] = "기본 가정값 7일 (역산 불가)"
    else:
        notes["overstock_source"] = "사용자 지정"
    low = (thresholds.get("low_stock_flag") or {}).get("threshold")
    notes["low_stock_cover_days"] = float(low) if low is not None else DEFAULT_LOW_STOCK_COVER_DAYS
    return replace(params, asof_date=pd.Timestamp(asof).normalize(), overstock_cover_days=float(over)), notes
