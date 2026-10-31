"""
해커톤 1일차 데이터 도구 패키지 (식자재 관리 및 발주 자동화).

모듈 구성 (자세한 설명은 src/README.md):
- schema.py      : 명세서 17개 파일의 선언적 스키마 (컬럼, 종류, 단위, 고유키, 외래키)
- parsers.py     : 목록/요일/시각/플래그 문자열 파서 (로더와 테스트가 함께 사용)
- loader.py      : CSV 탐색·읽기·형 변환 → Dataset (load_all)
- check_base.py  : 검증 결과(CheckResult)와 검증 공용 도우미
- checks_*.py    : 개별 검증 함수 묶음
- validate.py    : 검증 실행(run_all)과 보고서 작성(write_report)
- profile.py     : 테이블 프로파일 보고서(profile_all)
- prep.py        : 발주 엔진이 쓸 전처리 함수(일별 판매, 소요량, 단가, 발주단위, 공급사 달력 등)
- report_utils.py: 마크다운 표 작성 도우미
"""

__version__ = "0.1.0"
