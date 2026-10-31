"""
결정론적 발주 엔진 (기준선 버전). 모든 숫자는 이 패키지의 코드가 계산한다 (LLM 아님).

모듈
- params.py       : EngineParams, resolve_params (기준일·과잉 기준 채우기), 제공 플래그 역산
- forecast.py     : forecast_menu_daily — 같은 요일 평균 × 추세 × 공휴일 × 프로모션 × 취급 × 시나리오
- requirement.py  : ingredient_requirement (BOM), ingredient_sigma (메뉴 σ → 식자재 σ), menu_contributions
- projection.py   : project_inventory — 로트 FEFO 전개 (만료 폐기, 부족 = 품절), reproject_pair
- order.py        : recommend_orders — 공급사 달력·안전재고·발주단위·최소발주금액·폐기위험, format_explain
- alerts.py       : build_alerts — 품절위험 / 사용기한임박 / 과잉 (+ 제공 플래그 병기)
- alternatives.py : compare_alternatives — 정기 대기 / 긴급발주 / 매장 간 이동
- run.py          : run_engine → EngineOutput, briefing_template, summary_json, kpis
"""
from .params import EngineParams, resolve_params
from .run import EngineOutput, briefing_template, kpis, run_engine, summary_json

__all__ = ["EngineParams", "resolve_params", "EngineOutput", "run_engine", "briefing_template", "summary_json", "kpis"]
