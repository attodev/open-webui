# OpenWebUI 측 변경 사항 정리 — new-api SSO 연동 테스트

이 문서는 실제 통합 테스트 과정에서 **이미 구현되어 있던 new-api SSO 연동
코드**(별도 브랜치, 커밋 `84dc832d7`까지)에 대해, 실제로 new-api와 붙여서
테스트하다가 발견/수정한 내용을 정리한다. 원래 설계 문서는 다음 두 파일에
있다 (이 저장소 `docs/superpowers/` 아래):

- `specs/2026-08-17-newapi-oauth2-sso-integration-design.md`
- `plans/2026-08-17-newapi-oauth2-sso-integration.md`

## 상태: 아직 커밋되지 않음

아래 코드 변경 2건은 **로컬 워킹 디렉토리에만 있고 커밋되지 않았다**
(`git status`로 확인 가능). 검토 후 커밋 필요.

```
M backend/open_webui/routers/newapi_sso.py
M backend/open_webui/utils/newapi_oauth.py
```

## 1. 코드 수정: 이메일 없는 계정도 로그인 가능하도록

### 발견한 문제

new-api 쪽 계정은 이메일이 필수가 아니고, 실제로 관리자 테스트 계정
대부분이 이메일 없이 생성되어 있었다. 기존 코드는 `/oauth2/userinfo`
응답에서 `sub`와 `email`이 둘 다 있어야만 로그인을 허용했는데
(`fetch_userinfo`), 이메일이 비어있으면 매 로그인마다(최초 가입 때뿐
아니라 **이미 연결된 기존 계정의 재로그인 때도**)
`invalid_userinfo`로 실패했다.

### 수정 내용

**`backend/open_webui/utils/newapi_oauth.py` — `fetch_userinfo()`**
- 이제 `sub`만 필수로 검사. `email`은 없으면 빈 문자열로 취급하고 통과.
- `name`이 없을 때의 폴백도 `email` 대신 `f'new-api user {sub}'`로 변경
  (이메일 자체가 없을 수 있으므로).

**`backend/open_webui/routers/newapi_sso.py` — `_provision_or_login_user()`**
- new-api가 이메일을 안 줬을 경우, `sub` 기반으로 고정된 placeholder
  이메일(`newapi-{sub}@newapi.local`)을 생성해서 계정을 만든다.
  OpenWebUI 자체 스키마가 email 컬럼을 필수로 요구하기 때문에 필요한
  값일 뿐, 실제 신원 확인에는 쓰이지 않는다.
- 재로그인 시 계정 매칭은 원래부터 `sub` 기준(`get_user_by_oauth_sub`)이라
  이 placeholder 이메일이 나중에 계정을 잘못 연결하는 데 쓰이지 않도록,
  `merge_accounts_by_email` fallback 로직은 **실제 이메일이 있을 때만**
  작동하도록 명시적으로 막았다.

### 신규 계정의 role 문제 (같은 파일, 별도 수정)

이메일 문제를 고치고 나니 새로운 문제가 나타났다: 새로 생성된 계정이
`role: 'pending'`(관리자 승인 대기)으로 만들어져서 로그인은 되는데
"계정 활성화 대기 중" 화면만 나오는 문제.

- **원인**: `insert_new_auth` 호출 시 `role=await
  Config.get('ui.default_user_role')`을 그대로 썼는데, 이 배포의 전역
  기본값이 `pending`이었다.
- **수정**: SSO로 생성되는 계정은 전역 기본값을 쓰지 않고 **항상
  `role='user'`로 즉시 생성**하도록 고정. new-api가 이미 인증을 완료한
  사용자에게 OpenWebUI가 또 별도 승인을 요구하는 건 "new-api 하나로만
  로그인"이라는 이 통합의 설계 의도와 맞지 않기 때문. (최초 1명이
  admin이 되는 부트스트랩 로직은 그대로 유지)
- 이미 `pending`으로 만들어졌던 테스트 계정들은 DB에서 직접
  `role='user'`로 일괄 수정함 (코드가 아니라 데이터 수정 — 신규 저장소에는
  해당 없음).

## 2. 배포/설정 관련 (코드 아님, 운영 참고용)

로컬 테스트 환경(Docker, `python -m uvicorn` 직접 실행)에서 아래 항목들이
실제 운영 배포에도 반드시 필요하다는 걸 확인함. 코드 수정이 아니라
**환경변수/설정값**이다.

| 항목 | 값 | 이유 |
|---|---|---|
| `ENABLE_NEWAPI_SSO` | `true` | SSO 기능 자체를 켬 |
| `NEWAPI_OAUTH_BASE_URL` | new-api 호스트 URL | 토큰 교환/userinfo 호출 대상 |
| `NEWAPI_OAUTH_CLIENT_ID` / `NEWAPI_OAUTH_CLIENT_SECRET` | new-api 관리자가 발급한 값 | new-api 쪽 설정과 반드시 일치해야 함 |
| `NEWAPI_ENTRY_URL` | new-api 로그인 페이지 URL | 연결 끊김 시 재진입 안내에 사용 |
| `WEBUI_SECRET_KEY` | 임의의 긴 문자열 | 세션 JWT 서명 키. 미설정 시 기동 자체는 되지만 경고 발생 |
| `CORS_ALLOW_ORIGIN` | 실제 서비스 도메인 (또는 `*`, 테스트 한정) | **`dev.sh`가 이 값을 `localhost`로 하드코딩**하고 있어서, IP나 실제 도메인으로 접속하면 Socket.IO(WebSocket) 핸드셰이크가 origin 불일치로 거부됨 → 채팅 응답이 "생각 중..."에서 멈추는 증상으로 나타남. 운영 배포 시 `dev.sh`를 쓰지 않거나 이 값을 실제 도메인으로 맞춰야 함 |
| `BYPASS_MODEL_ACCESS_CONTROL` | `true` | OpenWebUI는 모델별로 별도 접근 권한(admin이 각 모델을 공개로 등록해야 함)을 갖고 있어서, 이걸 켜지 않으면 관리자 외 일반 사용자에게는 모델 목록이 빈 채로 보임. new-api가 이미 그룹/사용자별로 모델 접근을 통제하므로 이중 관리가 불필요하다고 판단하여 켬 |

### 프론트엔드 빌드 필요

`/auth` 등 SvelteKit 프론트엔드 라우트는 **백엔드만 띄우면 404**가 난다
(콜백 리다이렉트 자체는 성공해도 그다음 랜딩 페이지가 없음). 운영
배포에서는 `npm run build`(또는 최소 `npx vite build`, `pyodide:fetch`
스크립트는 이 기능과 무관하니 실패해도 무시 가능)로 프론트엔드를 빌드해서
`build/` 디렉토리를 백엔드가 서빙하도록 해야 한다.

## 3. 확인된 정상 동작 (수정 아님, 검증 결과)

- 콜백 → 계정 자동 생성/매칭 → OpenWebUI 세션 발급까지 전체 흐름 정상
- `is_admin` 클레임이 OpenWebUI의 `role: admin`으로 정확히 매핑됨
- 인증 코드 재사용 시 정확히 `invalid_grant`로 거부됨 (1회성 보장)
- new-api 로그아웃 시 다음 요청부터 즉시 재연결 필요 상태로 전환됨
- `auth_type: newapi_session` 커넥션 모드로 사용자별 토큰이 실제
  new-api 과금에 반영되는 것까지 확인 (동일 회선을 admin/일반 사용자
  각각의 토큰으로 직접 조회해서 대조)
