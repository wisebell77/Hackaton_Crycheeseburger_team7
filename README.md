# 재고의 내일 — 식자재 발주 의사결정 지원

크라이치즈버거 AX 해커톤 주제 1(식자재 관리 및 발주 자동화) 7팀. 판매·재고 데이터로 수요를 예측하고, 공급사 일정·사용기한·최소 발주금액을 반영한 발주안을 만든 뒤, 과거 예측 오차 시나리오로 품절·폐기 위험을 비교해 고르도록 돕는 웹 대시보드입니다. 데이터는 제공된 합성 데이터이며 금액은 가상 가격입니다.

## 폴더

| 경로 | 내용 |
|---|---|
| `web-dashboard/dist/` | 배포용 정적 웹 (빌드 없음). `app.js` 화면, `engine-core.js` 발주 엔진, `*.json` 가공 데이터 |
| `web-dashboard/scripts/` | 가공 데이터 재생성(Python)과 Node 검증 테스트 |
| `model/src/` | Python 엔진: 수요 예측, 식자재 소요량, 안전재고 σ, 재고 전개, 발주 추천, 발주안 평가 |
| `model/promotion_forecast/` | 행사(할인·특이사항) 반응을 반영한 수요 예측 |
| `model/order_policy/` | 자연어 성향 → 발주 정책, 후보 발주안 생성·순위 |
| `docs/promotion_analysis.md` | S02 세트 할인 행사(P01) 판매 변화 분석 |

## 바로 실행

```sh
python -m http.server 8000 --directory web-dashboard/dist
```

http://localhost:8000 → `초기 재고 9/30` → `발주 신청`. 원본 CSV나 Python 설치 없이 동작합니다.

배포는 `web-dashboard/dist` 폴더 전체를 정적 호스팅에 게시하면 됩니다 (빌드 명령 없음, 시작 문서 `index.html`). 일일 기록과 행사 조건은 브라우저 localStorage 에만 저장됩니다.

## 계산 흐름

1. **수요 예측** (`model/src/engine/forecast.py`, `model/promotion_forecast/`): 메뉴별 같은 요일 최근 4회 수요 평균 × 매장별 공휴일 계수(평일·주말 구분) × 취급 여부. 과거 행사일은 기본 수요에서 빼고, 점장이 넣은 행사 반응만 대상 메뉴·날짜에 곱합니다. BOM 으로 식자재 소요량으로 바꿉니다. 3~9월 매일 백테스트에서 식자재 1~3일 소요 오차(WAPE) 10.8%, 7일 이동평균은 13.3%.
2. **발주 추천** (`engine-core.js` ← `order.py`): 이번 발주 입고일부터 그다음 발주분 입고 전날까지(커버 기간)의 예측 소요 − 입고 전 부족분 + 안전재고 − 가용 재고(기한 지날 양 제외) − 기존 미입고 발주량을 포장 단위로 올림. 안전재고 = z(서비스 수준) × σ, σ는 같은 예측식의 식자재별 과거 예측 오차(커버 일수 합계 RMSE), 급변 품목 ×1.6.
3. **발주안 평가** (`engine-core.js` ← `evaluate.py`): 식자재별 과거 실제÷예측 비율 239개(7일 블록)를 이번 예측에 곱해 시나리오마다 재고를 사용기한 우선으로 소진. 품절 손실(메뉴별 가장 모자란 재료 비율 × 예측 판매 × 판매가)과 기한 경과 폐기 비용의 평균·나쁜 10%·품절 확률을 냅니다.
4. **성향별 발주안** (`engine-core.js` ← `order_policy` 프리셋·`candidates.py`): 균형(서비스 95%), 안전 추구(99%, 품절 가중 3배), 딱 경계만큼(90%, 폐기 가중 3배). 공급사 최소 발주금액 미달 시 '다음 발주일로 연기'와 '소진 빠른 품목부터 1단위씩 증량' 중 성향 점수가 좋은 안을 고릅니다. 점수 = (1 − 꼬리 비중) × 평균 손실 + 꼬리 비중 × 나쁜 10% 평균 손실.

브라우저 엔진(`engine-core.js`)은 같은 입력에서 Python 엔진과 발주 수량·안전재고·위험 지표가 일치하도록 `test-engine.cjs` 로 대조합니다.

## 가공 데이터 재생성

제공 원본 CSV·zip 은 저장소에 올리지 않습니다 (해커톤 이용 조건). 비공개 폴더에 두고 저장소 최상위에서 실행합니다.

```sh
pip install -r model/requirements.txt
python web-dashboard/scripts/prepare_data.py --data-zip <원본 zip>              # data.json, purchase-history.json
python web-dashboard/scripts/export_promotion_forecasts.py --data <CSV 폴더>    # team-forecasts.json, promotion-inputs.json
python web-dashboard/scripts/export_engine_params.py --data <CSV 폴더>          # engine-params.json, 대조 기준값
```

판매 관측 마지막 날이 바뀌면 `export_promotion_forecasts.py` 의 관측 종료일(2026-09-30)과 기준일 범위를 함께 고쳐야 합니다.

## 검증

```sh
node web-dashboard/scripts/test-engine.cjs           # 브라우저 엔진 = Python 엔진 (추천 198건·평가 6건) + 후보 규칙
node web-dashboard/scripts/test-promotions.cjs       # 행사 입력 검증·BOM 환산
node web-dashboard/scripts/test-team-forecasts.cjs   # 예측 창·재고 불변
cd model && python -m pytest tests -q                # 엔진·정책 테스트 (가짜 데이터)
```

실데이터 테스트는 `PROMOTION_TEST_DATA=<CSV 폴더>`, `POLICY_TEST_DATA=<CSV 폴더>` 를 지정하면 함께 돕니다.

## Python 엔진 단독 실행 (`model/`)

| 명령 | 내용 |
|---|---|
| `python run_forecast.py --data <CSV>` | 예측·소요량·σ·재고 전개·발주 추천·알림 → `forecast_out/` |
| `python run_evaluate.py --data <CSV> --scale 1.3 --plan my_plan.csv` | 발주안 평가·비교 → `eval_out/` |
| `python run_promotion_forecast.py --data <CSV> --asof 2026-09-30 --promotions promotion_plan.example.json` | 행사 반영 예측 → `promotion_out/` |
| `python run_policy.py --data <CSV> --owner '안전하게 주문하자' --order 'S03만, 20만원 미만'` | 자연어 → 정책 JSON |
| `python rank_policy.py --data <CSV> --asof 2026-09-23 --owner ... --order ...` | 후보 생성·순위 → `policy_out/rank/` |

## 가정과 한계

- 판매 관측은 2026-09-30까지이고 10/14 기준일까지 7일 예측을 제공합니다. 9/30 이후 기준일도 같은 σ를 써서 위험이 다소 작게 보일 수 있습니다.
- 행사 반응은 점장 가정 또는 같은 매장·메뉴 과거 행사(P01, 1건)의 전이 가정입니다. 할인율만으로 수요 증가를 추정하지 않습니다.
- 품절 손실은 판매가 기준이라 이익이 아니고, 동시에 모자란 재료는 메뉴 단위로 한 번만 셉니다. 발주 금액은 손실에 더하지 않습니다.
- 지연·부분 입고, 품질 폐기·실사 차이·매장 간 이동, 긴급발주 후보는 미래 계산에 넣지 않았습니다. 다음 발주 이후의 발주는 계획하지 않습니다.
- CSV 다운로드는 추천 초안이며 실제 공급사로 전송되지 않습니다. 기록은 기기 간 공유되지 않습니다.
