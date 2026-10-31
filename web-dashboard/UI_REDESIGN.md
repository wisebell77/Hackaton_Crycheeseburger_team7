# 크라이치즈버거 대시보드 UI

기준: `main`의 `31efbbc6ecfa3f618359cb42d195b62fa246716b` (설정 기능 머지 포함).

## 구성

기존 HTML/CSS/JavaScript, Python 자연어 API 구조를 유지했습니다. React 전용 shadcn/ui와 Tailwind 빌드를 추가하지 않았습니다. 아이콘은 Lucide 스타일의 의존성 없는 인라인 SVG입니다.

- 공식 로고: `assets/crycheese-logo.webp` (https://www.crycheeseburger.com/ 에서 사용 중인 로고)
- 브랜드 기준: 노란색 `#F8B81C`, 검정 헤더, 흰색 카드.
- 상단 브랜드 메뉴, 좌측 운영 메뉴, 발주 추천, 요청 입력, 위험 요약, 품목 테이블 순으로 정리했습니다.
- 모바일은 390px 폭에서 확인했으며 메뉴는 하단에 배치합니다. 표는 영역 안에서 가로 스크롤합니다.
- 기본 성향·발주 기준·한도·저장 설정은 유지하고 설명을 펼쳐보기로 정리했습니다.
- Adobe는 여백과 타이포그래피의 명료함만 참고했습니다. Adobe의 로고·이미지·색상·콘텐츠는 사용하지 않았습니다.

## 유지한 기능

기존 API 경로, JSON 파일, localStorage 키, DOM ID, 설정 저장과 병합 규칙을 유지했습니다.
수요 예측/발주/평가/정책 엔진, 예산·MOQ·제외 품목 검사는 변경하지 않았습니다.
주문 버튼은 기존처럼 검토 창과 CSV 저장을 제공합니다. 실제 공급사 주문은 전송하지 않습니다.

## 변경 파일

- `dist/index.html`: 브랜드 헤더/사이드바 구조, 접근 가능한 아이콘, 화면 문구.
- `dist/brand.css`: 반응형 레이아웃과 시각 스타일. 기존 CSS 다음에 로드합니다.
- `dist/assets/crycheese-logo.webp`: 공식 로고.
- `dist/app.js`: 화면 제목·설명·차트 색상. 불필요한 무위험 안내를 숨깁니다.
- `dist/settings-ui.js`: 추천 카드/설정 창의 문구와 마크업. 상태/이벤트/계산은 유지합니다.
- `dist/promotion-ui.js`, `daily-inventory.js`, `purchase-history.js`: 간결한 제목과 안내.

## 실행

```bash
python3 web-dashboard/serve.py
```

http://localhost:8000

## 검증

```bash
node web-dashboard/scripts/test-engine.cjs
node web-dashboard/scripts/test-promotions.cjs
node web-dashboard/scripts/test-team-forecasts.cjs
node web-dashboard/scripts/test-policy-ui.cjs
node web-dashboard/scripts/test-ui-startup.cjs
PYTHONPATH=model python3 -m unittest discover -s model/tests -p test_order_policy.py -q
```

Chrome의 지원되는 HTTP 미리보기에서 실제 배포 파일을 렌더링했습니다. 설정 저장, 검색, 품목 상세, 발주서 검토·CSV 다운로드, 재고 기록, 발주 이력 조회를 확인했습니다. 390px iframe 뷰포트에서 전체 페이지 가로 넘침이 없음을 확인했습니다. 자유 문장 API 외부 제공사 실호출은 키 없이 검사하지 않았고 해당 연결 코드는 변경하지 않았습니다.
