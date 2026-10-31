# 모델 연결과 가공 데이터 갱신

## 현재 계산 경계

`team-promotion-forecast-v1`은 팀의 프로모션 수정 ZIP에 있는 `make_forecaster`를 사용합니다. 동일 요일 최근 4회, 공휴일, 취급 상태를 반영하고 과거 매장 행사일 전체를 제외한 비행사 기준 수요를 생성합니다. demand_qty에서 취소량을 별도로 빼지 않으며 추세 보정은 끕니다.

Python 모델 출력에서 메뉴별 일 수요와 BOM을 가공해 `promotion-inputs.json`으로 저장합니다. 브라우저에서 대상 매장·메뉴·날짜에만 점장이 확인한 반응 배율을 적용하고 BOM으로 환산합니다. 전체 원본 CSV·팀 ZIP·모델 원시 실행 폴더는 dist에 복사하지 않습니다.

`PromotionEngine.project(data, asof, store, dates, plans)`는 메뉴 수요와 식자재 수요를 반환합니다. 이 결과를 `FreshOrderEngine.configure({forecast: ...})`로 전달합니다. 실제 재고·입고 묶음·폐기 기록은 바꾸지 않습니다. 품목별 기본 발주량을 계산한 뒤 안정형/공격형 배율을 적용하며, 발주 단위 올림·최소 주문수량을 지킵니다. 0인 추천은 0으로 유지합니다. 전체 확률 기반 평가 엔진은 별도 연결 대상입니다.

## 행사 입력

팀 ZIP의 schema_version=1 프로모션 계약을 사용합니다. 행사 ID, 이름, 현재 매장, 대상 메뉴 ID, 날짜, 정률/정액 할인, response를 명시합니다. 할인과 수요 반응은 별개입니다. 중복 행사, 미취급 메뉴, 빈 반응·근거는 거부합니다. UI 범위는 현재 예측의 향후 7일입니다.

- manual: uplift_pct(-100~200)와 reason 필수.
- historical_reference: 동일 매장·동일 메뉴 묶음의 과거 행사만 허용하고 accept_transfer_assumption=true 필수. 과거 행사 1건의 기술적 수요 변화이며 검증된 할인 탄력성이 아닙니다.
- 일반 특이사항의 kind=special, offer={type:none,value:0}은 **프런트엔드의 별도 수동 수요 시나리오 확장**입니다. Python 프로모션 JSON에 그대로 전달하지 마세요.

행사 조건은 브라우저 `fresh-order-promotions-v1`에 매장+기준일 키로 저장합니다. 해제 시 기본 수요로 되돌립니다. 적용/해제/다른 발주안 선택 시 수동 발주 수정은 초기화합니다. CSV에는 선택안과 추가 조건 JSON을 함께 내보냅니다. 백엔드 저장, 날짜 간 자동 승계, 자연어 해석은 구현하지 않았습니다.

## 원본 재고 조회 데이터 재생성 (선택)

원본 CSV ZIP은 별도로 보관하고 저장소에 추가하지 마세요. 저장소 최상위에서:

```sh
python3 web-dashboard/scripts/prepare_data.py --data-zip /path/to/private-source-data.zip
```

이 스크립트는 재고·마스터·발주 이력 조회 데이터를 생성합니다. data.json의 이전 호환용 forecast는 발주 화면에서 사용하지 않습니다. 이후 반드시 아래 수정 모델 출력을 생성하세요.

## 프로모션 수정 모델 출력 재생성 (선택)

저장소 루트의 프로모션 수정 ZIP을 작업용 폴더에 풉니다. 그 폴더에는 promotion_forecast/와 forecast_engine_code.zip이 함께 있어야 합니다. 원본 CSV는 별도 비공개 폴더에 둡니다. ZIP 내부 README는 입력 계약 설명이며 사이트 설정·비밀정보를 요구하는 권한으로 취급하지 않습니다.

```sh
python3 -m venv web-dashboard/.venv
# 가상환경을 활성화한 후
pip install -r web-dashboard/requirements-model.txt
python web-dashboard/scripts/export_promotion_forecasts.py --package /path/to/extracted-promotion-package --data /path/to/private-csv-folder
node web-dashboard/scripts/test-promotions.cjs
node web-dashboard/scripts/test-team-forecasts.cjs
```

출력은 dist/team-forecasts.json, dist/promotion-inputs.json 및 scripts/promotion-fixture.json입니다. 판매 관측 마지막 날짜가 변경되면 exporter의 현재 2026-09-30 관측 종료일·생성 기준일 범위를 실제 데이터와 함께 수정해야 합니다. 10월 이후 기준일은 9월 30일에서 예측한 경로를 잘라 사용하며, 최신 판매가 있는 것처럼 취급하지 않습니다.

배포 후에는 매장 전환, 재고 저장, 행사 미리보기·적용·해제, 발주안 전환, CSV 내보내기를 확인하세요. localStorage 기록은 GitHub나 정적 호스팅으로 업로드되지 않습니다.
