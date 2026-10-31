# 사장 기본 성향 + 이번 발주 요청 → 발주 정책

이 코드는 팀의 **3번 의사결정 단계에 넣을 정책 계층**이다. 두 엔진 ZIP을 실제로 풀어 `EngineParams`, `run_engine`, `evaluate_plan`, `PlanEvaluation.scenarios`의 인터페이스를 확인해 연결했다. AI 학습은 없다. 실제 발주 전송, 웹 UI, 모든 가능한 수량의 최적화 탐색은 이 모듈의 범위가 아니다.

## 1. 입력·병합 규칙

- 첫 칸: 사장 기본 성향. 앱 설정에 저장하고 매 요청마다 전달한다.
- 둘째 칸: 이번 주문만의 요청. **동일 필드의 명시적 요청 > 기본 성향 > 시스템 기본값**.
- 둘째 칸에 언급하지 않은 기존 예산/확률 한도는 유지한다. 예산을 명시하면 이번 요청의 예산으로 교체한다.
- `mode`를 이번에 바꾸면 성향 프리셋의 소프트 파라미터를 교체한다. 별도 숫자는 그 뒤 적용한다.
- 제외 품목은 기본 성향과 이번 요청의 합집합. 해제는 별도 UI에서 명시적으로 처리해야 하며 이 파서는 임의 해제하지 않는다.
- 이 라이브러리는 기본 성향을 디스크/DB에 저장하지 않으므로 이번 요청이 영구 설정을 바꾸지 않는다.
- 요청 내부 충돌/지원 밖 조건/모호한 ID는 `status=needs_clarification`. 값 일부가 보이더라도 이 상태로 실행할 수 없다.
- 모든 변경에 원문 근거, 이전 값, 이후 값, owner/order 출처가 `audit`에 남는다.

예: 기본 `안전하게 주문하자, 30만원 이하` + 이번 `S03만, 20만원 언더, 10% 여유, 우유 제외`.
결과: safe 성향 유지, 이번 발주 합계 **200,000원 미만**, 매장 S03, 우유 I016 발주 금지, 커버 구간 수요의 10% 추가 여유. 기본 설정은 바뀌지 않는다.

## 2. 파라미터: 성향·상황·하드 제약을 분리

|분류|필드|범위/기본|계산에서의 역할|
|---|---|---|---|
|성향|`mode`|balanced / safe / lean|모호한 성향을 공개된 프리셋으로 매핑|
|성향|`service_level`|0.5~0.9999, 기본 0.95|기존 엔진의 z×σ 안전재고. 실현 품절 확률을 보장하지 않음|
|성향|`extra_demand_buffer_ratio`|0~0.5, 기본 0|커버 구간 총수요 대비 추가 여유. 현재고/입고를 차감한 뒤 포장 올림|
|성향|`shortage_weight`|0~10, 기본 1|기존 엔진의 품절 손실매출에 적용하는 상대 선호 가중치|
|성향|`waste_weight`|0~10, 기본 1|폐기 재료비에 적용하는 상대 선호 가중치|
|성향|`tail_weight`|0~1, 기본 0.2|평균 손실과 CVaR의 혼합 비중|
|성향|`cvar_alpha`|0.5~0.99, 기본 0.9|0.9이면 나쁜 쪽 10% 시나리오의 평균 손실|
|상황|`demand_multiplier`|0.1~3, 기본 1|명시적 수요 증가/감소만 예측에 곱함. '안전하게'로 올리지 않음|
|상황|`delay_buffer_days`|정수 0~7, 기본 0|기존 엔진이 지원하는 **기존 미입고 PO** 지연 가정. 신규 발주 배송 지연을 뜻하지 않음|
|하드 제약|`budget`|null 또는 `{won,operator}`|선택 매장 전체의 이번 신규 발주 합계. `lt`는 미만, `le`는 이하|
|하드 제약|`max_stockout_probability`|null 또는 0~1|평가 창 안에서 어느 품목이든 부족한 시나리오의 비율 상한|
|하드 제약|`max_expected_waste_won`|null 또는 0 이상|평가 엔진의 기대 폐기 재료비 상한. 기존 재고의 폐기도 포함|
|하드 제약|`allow_emergency`|기본 false|긴급발주 후보를 허용하는지. true여도 이 모듈이 긴급안을 자동 생성하지는 않음|
|하드 제약|`store_ids`|기본 null=전체|01 CSV에서 선택 범위 검증. 예산/확률/손실의 집계 범위도 함께 제한|
|하드 제약|`excluded_ingredient_ids`|기본 []|03 CSV의 식자재를 신규 발주에서 제외. 수요 자체를 없애지는 않음|

### 프리셋 v1 (추정값이 아닌 조정 가능한 제품 기본값)

|성향|서비스 수준|품절 가중치|폐기 가중치|꼬리위험 비중|
|---|---:|---:|---:|---:|
|안전 safe|0.99|3|1|0.4|
|균형 balanced|0.95|1|1|0.2|
|절약 lean|0.90|1|3|0.1|

세 프리셋 모두 여유율 기본 0, CVaR 수준 0.9. 서비스 수준과 여유율을 동시에 자동으로 크게 올려 이중으로 과발주하지 않는다. 명시적으로 '10% 여유'를 입력하면 추가 적용한다.

### 여유율의 정확한 의미

```
순필요량 = max(0,
    커버 구간 예상소요 - 입고 전 이미 놓치는 수요 + 안전재고
    + 여유율 × 커버 구간 예상소요
    - 사용 가능한 현재고 - 입고 예정량)
발주량 = 순필요량 / 발주단위당 사용량 → 포장 단위 올림 → 양수일 때 품목 MOQ 적용
```

기존 발주량 전체에 1.1을 곱하는 방식이 아니다. 이미 재고가 충분하면 여유율이 있어도 발주 0이 될 수 있다. 금지 품목은 후보에서 0으로 만들고, 그로 인한 품절 위험은 그대로 평가한다.

### 비교 점수

시나리오별 L = 품절가중치 × 손실매출 + 폐기가중치 × 폐기재료비.
점수 = (1−꼬리위험비중) × 평균(L) + 꼬리위험비중 × CVaR(L).
예산/운영 제약을 먼저 통과한 후보만 점수 비교. 동점이면 발주 금액이 낮은 안을 선택한다.

발주 지출 전체를 손실에 다시 더하지 않는다. 판매 가능한 재고의 취득비와 폐기 재료비를 중복 손실로 다루지 않기 위해서다. 손실매출은 이익이 아니며 이 점수는 회계적 순이익이 아닌 의사결정용 지표다.

## 3. CSV에서 읽는 변경 불가능한 제약

|파일|적용|
|---|---|
|01_stores|유효 매장, 매장 범위|
|03_ingredients|유효 식자재, 공급사, 포장/사용단위 환산, 품목별 MOQ|
|05_suppliers|발주 가능 요일, 마감, 리드타임, 배송 요일, 최소 발주금액|
|06_ingredient_price_history|발주일에 유효한 포장당 단가|
|그 밖의 17개 데이터|기존 1·2번 엔진의 수요·BOM·재고·입고·만료·시나리오 평가에 사용|

실제 CSV에서 확인: I016 우유는 use_unit=ml, order_unit=EA, 1EA=1,000ml. I009 양상추는 1BOX=8,000g. 공급사 SUP_A는 매장별 발주서 최소 120,000원, SUP_B는 0원. **이 값들은 코드에 하드코딩하지 않고 데이터에서 읽는다.**

최소금액 그룹은 매장×공급사×발주일×발주유형. 여러 매장의 발주를 합쳐 A사의 최소금액을 만족시킨 것처럼 처리하지 않는다. 양수 수량은 정수 포장 단위. 원본 엔진이 1.5를 1로 자르는 정규화를 하므로, 어댑터에서 먼저 거부한다. 단가 누락을 0원 처리하지 않는다. 원본 ZIP 엔진 소스는 변경하지 않았다.

## 4. 실행

Python 3.10 이상. 정책 추출만은 표준 라이브러리로 실행된다. 엔진 연결은 원본 requirements의 pandas/numpy가 필요하다.

```bash
# 저장소/이 배포 폴더의 두 엔진 ZIP 자동 해제
python unpack_engines.py

# 정책만 생성 (01,03 CSV로 실제 이름/ID 검증)
python run_policy.py --data /path/to/csv_folder \
  --owner '안전하게 주문하자' \
  --order 'S03만, 20만원 언더, 10% 여유, 우유 제외' \
  --out policy_out/policy.json

# 실제 1·2번 엔진에 연결한 후보 평가 예제
pip install -r engines/forecast_engine_code/requirements.txt
python evaluate_policy.py --engine-dir engines/forecast_engine_code \
  --data /path/to/csv_folder --asof 2026-09-23 \
  --owner '안전하게 주문하자' --order 'S03만, 20만원 미만, 10% 여유'
```

`evaluate_policy.py`는 추천안에 여유율을 적용한 **후보 하나**를 평가하는 연결 예제다. 예산을 넘으면 실패 상태를 반환하고, 예산에 맞추려고 수량을 임의로 줄이지 않는다. 후보 탐색은 다음 개발 단계에서 구현한다. 종료 코드 0=통과, 2=자연어 해석 문제, 3=후보 제약 불충족.

업로드 데이터의 이력은 9/30까지다. 예제는 미래 취급기간 밖의 외삽을 피하도록 9/23을 기준일로 사용한다. 기본 자연어의 '이번'은 항상 엔진의 판단 시각을 뜻하며 컴퓨터의 현재 날짜를 자동으로 쓰지 않는다.

### 앱 연결

```python
from order_policy import compile_policy, rank_candidates
from order_policy.engine_adapter import (
    load_engine, catalog_from_dataset, scope_dataset, engine_params,
    buffered_plan, evaluate_with_policy, candidate_from_evaluation,
)

load_engine('engines/발주안_평가엔진')  # 프로세스당 한 엔진
from src.loader import load_all
from src.engine import run_engine

full = load_all('data')
policy = compile_policy(owner_text, order_text, catalog=catalog_from_dataset(full))
if policy['status'] != 'ready':
    # issues를 UI에 표시하고 실행 중단
    raise ValueError(policy['issues'])
ds = scope_dataset(full, policy)
params = engine_params(policy, asof_date='2026-09-23')
out = run_engine(ds, params)
plan = buffered_plan(out, policy)
# evaluate_policy.py의 daily/scenarios 생성 부분을 사용.
# 같은 정책/매장범위/평가창/시나리오로 모든 후보를 평가한다.
report, ev = evaluate_with_policy(ds, out, plan, policy, scenarios=scenarios, daily=daily)
```

1번 예측과 과거 시나리오는 캐시할 수 있다. 수요 배율·기존 PO 지연·매장 범위가 바뀌면 해당 계산 캐시를 갱신해야 한다. 순수 가중치/예산만 바뀌면 이미 평가한 같은 후보의 시나리오를 재채점할 수 있다. 서비스 수준·여유율은 후보 수량을 바꾸므로 후보를 다시 만들어 평가해야 한다.

## 5. 자유로운 자연어 API 연결

규칙 추출기는 키 없이 즉시 사용 가능하지만 한정된 문법만 지원한다. `20만원 이하/미만/언더/안에서`, `10% 여유`, `수요가 20% 증가`, `품절 확률 10% 이하`, `폐기비 5천원 이하`, `S03만`, `우유 제외`, `긴급발주 금지/허용` 등이 가능하다.

자유로운 문장에는 `CompatibleAPIExtractor`를 사용한다. 외부 API는 의미 추출만 하고, 실제 금액/수량 계산·제약 검사는 Python이 한다. 무료 제공량이 있는 공급사를 사용하더라도 비용/한도는 그 공급사의 설정을 따른다. 임의의 무료 서버나 API 키를 코드에 넣지 않았다.

```bash
export POLICY_API_URL='https://YOUR_PROVIDER/COMPATIBLE_CHAT_COMPLETIONS_PATH'
export POLICY_API_MODEL='YOUR_MODEL'
export POLICY_API_KEY='YOUR_KEY'
python run_policy.py --api --data /path/to/csv_folder \
  --owner '안전하게 주문하자' --order '이번엔 총 20만원 이하로 부탁해'
```

혹은 `compile_policy(..., extractor=CompatibleAPIExtractor())`. API에는 두 문장을 각각 추출하도록 보내며 원본 판매/재고 CSV는 보내지 않는다. 이름 해석용 매장·식자재 ID/이름 목록만 보낸다. 외부 API 실호출은 키가 없어 검증하지 않았고, 모의 HTTP 응답으로 오류·추출 계약을 검사했다.

API가 임의의 코드/SQL/새 필드를 반환해도 실행하지 않는다. 근거 문장의 존재, 숫자, 단위, 범위, 필드 이름, ID를 검사한다. 그렇더라도 자연어 의미 해석이 완벽하다는 보장은 없으므로 UI에서 적용 정책·근거를 보여줘야 한다. API 실패 시 요청을 조용히 무시하는 대신 차단한다.

현재 미지원: 메뉴/품목별 별도 서비스 수준, 특정 날짜에만 수요 배율, 품목별 수량 상한/고정, 수량 하한, 냉장 용량 제한, 특정 시각까지 입고, 신규 발주 배송지연 모형, 실제 이동 실행. 이런 요청을 임의 파라미터로 축약하지 않고 issues로 보낸다. 규칙 파서는 임의의 한국어 전체를 이해하는 파서가 아니므로 자유 문장 입력에는 API 모드를 연결해야 한다.

## 6. 검증·실무 한계

```bash
python -m unittest discover -s tests -v
# 실제 17개 CSV로 기존 엔진 연결까지 확인
POLICY_TEST_DATA=/path/to/csv_folder python -m unittest discover -s tests -v
# 두 번째 엔진도 별도 프로세스에서 확인
POLICY_TEST_DATA=/path/to/csv_folder POLICY_TEST_ENGINE=engines/forecast_engine_code \
  python -m unittest discover -s tests -v
```

확인 항목: 요청 우선순위, 제외 품목, 소수 포장 거부, 예산 미만/이하 경계, 공급사 최소금액, 과거 날짜/잘못된 입고일, 금지 긴급발주, 숫자 환각, 빈 시나리오, CVaR 경계 표본, 실제 예산 초과 탈락, 우유 포장 환산, 선택 매장 외 수요 배제.

평가 결과는 기존 2번 엔진의 근사 모형을 따른다. 품절 손실매출은 메뉴별 가장 부족한 재료의 부족 비율을 사용하는 근사치다. 현재 엔진은 식자재별 책임기간과 14일 폐기기간을 사용하며, 평가기간 밖 폐기를 이 모듈이 보완하지 않는다. 서비스 수준 99%라도 전체 품목 중 하나 이상 부족할 확률은 크게 나올 수 있다. 실제 데이터를 사용한 모의 확률이지 현실의 확률 보장은 아니다. 후보가 조건을 만족하지 못하면 `no_feasible_candidate`이고 예산·공급사 규칙을 자동 완화하지 않는다.

원본 CSV와 API 키는 배포 ZIP/저장소에 포함하지 않는다.
