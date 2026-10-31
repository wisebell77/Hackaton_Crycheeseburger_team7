# 크라이치즈버거 대시보드 UI

디자인 기준: `design/crycheese-minimal-ui` 브랜치(`9980393`)의 간소화 UI 시안([Figma Make](https://www.figma.com/make/nyABaEYw6aPVioLJIU7KU1)). 시안 폴더의 안내대로 기존 HTML·JavaScript를 덮어쓰지 않고 테마를 기존 스타일 다음에 불러옵니다.

## 구성

- `dist/cry-ui/theme.css`, `dist/cry-ui/assets/`: 디자인 브랜치 파일 그대로. 노란색 `#F8B81C`, 짙은 상단 메뉴, Pretendard Variable 글꼴(SIL OFL 1.1, `assets/OFL.txt`), 공식 로고·캐릭터, 반복 설명 숨김.
- `dist/cry-ui/app.css`: 시안에 없는 최신 앱 영역의 보정. 추천 발주안 카드(이번 요청 포함)를 시안의 선택된 발주안처럼 노란 강조로, 다른 발주안을 행사 입력과 같은 접힘 상자로 표시합니다. 화면 순서는 매장·날짜 → 핵심 숫자 → 추천 발주안 → 다른 발주안 → 행사 → 품목 표입니다.
- 불러오는 순서: `styles.css` → `settings.css` → `brand.css` → `cry-ui/theme.css` → `cry-ui/app.css`. `brand.css`는 설정 창·상세 패널·재고 기록 화면의 구성요소 스타일로 남아 있고, 겹치는 부분은 테마가 덮습니다.
- `dist/index.html`: 왼쪽 메뉴를 없애고 시안의 상단 메뉴(발주 신청 / 일일 재고 기록 / 과거 발주 이력)로 바꿨습니다. 설정과 데이터 기준(ⓘ)은 상단 오른쪽에 있습니다.
- `dist/app.js`: 화면 이름을 메뉴 이름과 맞췄습니다.

## 유지한 기능

API 경로, JSON 파일, localStorage 키, DOM ID, 설정 저장·병합 규칙, 수요 예측·발주·평가·정책 엔진은 바꾸지 않았습니다. 주문은 추천 발주안 카드의 주문 버튼에서 검토 창과 CSV 저장으로 이어집니다. 실제 공급사 주문은 전송하지 않습니다.

## 실행과 검증

```bash
python3 web-dashboard/serve.py
node web-dashboard/scripts/test-ui-startup.cjs
```

나머지 검증 명령은 저장소 최상위 README를 따릅니다. 1440px·390px 화면에서 발주 신청·일일 재고 기록·과거 발주 이력·설정 창을 확인했고, 390px에서 페이지 가로 넘침은 없습니다.
