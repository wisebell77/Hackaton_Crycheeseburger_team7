# Hackaton_Crycheeseburger_team7

식자재 수요 예측 · 재고 관리 · 발주안 검토 해커톤 프로젝트입니다.

## 웹 대시보드

- [소스와 실행·배포 안내](web-dashboard/README.md)
- [모델 연결과 데이터 갱신](web-dashboard/MODEL_INTEGRATION.md)
- 현재 시연: https://fresh-order-desk-1031.sizedazure1.chatgpt.site/

```sh
python3 -m http.server 8000 --directory web-dashboard/dist
```

http://localhost:8000 에서 실행됩니다. 정적 배포 대상은 web-dashboard/dist 전체입니다. 화면은 제공된 가공 시연 데이터로 실행되며 실제 주문을 전송하지 않습니다.

## 팀 모델 자료

기존 forecast_engine_code.zip, 발주안 평가 엔진 ZIP, order_policy_code.zip, 프로모션 수정 ZIP 및 각 설명 문서는 기존 경로에 유지했습니다. 대시보드에 연결된 범위와 아직 연결하지 않은 평가·정책 기능은 위 모델 연결 안내에 구분해 두었습니다.
