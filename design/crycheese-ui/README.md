# 크라이치즈버거 · 간소화 UI 디자인 코드

[Figma Make 시안](https://www.figma.com/make/nyABaEYw6aPVioLJIU7KU1)을 팀원이 기존 앱에 연결할 수 있도록 정리한 디자인 자료입니다.

이 폴더는 독립된 UI 미리보기입니다. 발주 엔진·API·실제 재고 데이터·로그인 설정을 포함하지 않습니다. 기존 `web-dashboard/` 파일을 교체하는 패키지가 아닙니다.

## 미리보기 실행

저장소 최상위에서 다음 명령을 실행한 뒤 http://localhost:8000 을 엽니다.

```sh
python3 -m http.server 8000 --directory design/crycheese-ui
```

예시 숫자는 화면 배치 확인용입니다. 미리보기의 버튼은 화면 선택 등 UI 동작만 수행합니다.

## 파일

| 파일 | 용도 |
| --- | --- |
| `index.html` | 화면 구조와 컴포넌트 배치를 확인하는 예시 HTML |
| `theme.css` | 노란색·폰트·상단 메뉴·설명 숨김·간소화 스타일 |
| `preview-base.css` | 미리보기에서 사용하는 기본 컴포넌트 스타일 |
| `preview.js` | 미리보기의 화면 선택 등 UI 동작. 업무 계산 코드가 아님 |
| `assets/` | 공식 로고·캐릭터·Pretendard Variable 글꼴 |

## 적용 담당자에게

1. 최신 기능 브랜치에서 별도의 통합 브랜치를 만듭니다.
2. 이 폴더의 `index.html`을 레이아웃 참고로 사용합니다. 최신 앱의 HTML·JavaScript 파일을 이 예시로 통째로 덮어쓰지 않습니다.
3. 최신 앱에 `theme.css`와 `assets/`를 옮긴 뒤 기존 스타일 다음에 테마를 로드합니다. 예를 들어 `web-dashboard/dist/cry-ui/`에 테마와 assets를 옮겼다면:

   ```html
   <link rel="stylesheet" href="styles.css">
   <link rel="stylesheet" href="cry-ui/theme.css">
   ```

4. `theme.css`는 기존 화면의 클래스·ID에 적용되는 스타일입니다. 아래 표를 기준으로 최신 마크업에 맞게 선택자를 조정합니다. 최신 앱에서 추가된 설정·정책 입력·예산·오류 표시 영역을 숨기지 않도록 확인합니다.
5. `preview-base.css`와 `preview.js`는 미리보기용입니다. 운영 앱에는 최신 컴포넌트 스타일과 이벤트 처리를 우선 사용합니다.
6. 데이터 연결, 입력 검증, 발주 계산, API 호출과 저장 처리는 최신 앱의 구현을 유지합니다. 필수 경고와 접근성 label/aria-label도 유지합니다.

| 영역 | 테마 선택자 예시 |
| --- | --- |
| 상단 메뉴 | `.sidebar`, `.brand-nav-inner`, `.brand`, `.nav-item` |
| 매장·날짜 선택 | `.controls`, `.store-tabs`, `.date-control` |
| 핵심 지표 | `.metrics`, `.metric`, `.metric-value` |
| 발주안 선택 버튼 | `.order-plan-cards`, `.order-plan` |
| 행사 조건 | `#promotionSection`, `#promotionEditor` |
| 품목 표 | `.inventory-panel`, `.inventory-heading`, `.filter-row`, `.table-wrap` |
| 설명 숨김 | `theme.css` 끝의 `Minimal UI` 구역 |

테마가 연결하는 글꼴 경로는 `assets/pretendard.css`, 글꼴 파일 경로는 `assets/fonts/`입니다. 테마와 assets의 상대 위치를 함께 유지하세요.

## 디자인 범위

- 짙은 상단 메뉴와 노란 선택 버튼
- 큰 배너·캐릭터 말풍선·반복 설명 제거
- 매장/날짜, 핵심 숫자, 발주안 버튼, 품목 표 중심의 배치
- 자세한 계산 설명은 정보 버튼에서 확인
- 좁은 화면에서도 선택 버튼과 표를 사용할 수 있는 반응형 스타일

## 출처

- 기준 시안: https://www.figma.com/make/nyABaEYw6aPVioLJIU7KU1
- 브랜드 참고: https://www.crycheeseburger.com/
- 로고: https://crycheeseburger.com/images/brand/logo-with-text.webp
- 캐릭터: https://crycheeseburger.com/images/brand/emblem.webp
- 글꼴: [Pretendard](https://github.com/orioncactus/pretendard), SIL Open Font License 1.1 (`assets/OFL.txt`)

현재 브랜치는 디자인 자료를 추가하기 위한 것입니다. 사이트 적용·배포는 통합 담당자가 별도로 진행합니다.
