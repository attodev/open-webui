# new-api OAuth2 SSO 연동 설계

- 상태: 브레인스토밍 승인 완료, 사용자 리뷰 대기
- 관련 문서:
  - `2026-08-16-oauth2-openwebui-integration-handoff.md` (new-api 팀이 전달한 원본 연동 계약서, 메인 워크트리 `docs/`에 위치)
  - `2026-08-17-openwebui-response-to-newapi-oauth2-handoff.md` (본 설계를 바탕으로 new-api 팀에 보낸 확인/요청 문서, 메인 워크트리 `docs/`에 위치)

## 배경 및 목표

OpenWebUI를 독립적인 로그인 시스템으로 두지 않고, new-api(LLM 게이트웨이/자산관리 플랫폼)에 완전히 종속시킨다:

- 로그인/로그아웃은 new-api에서만 이루어진다. OpenWebUI는 자체 회원가입/로그인 화면을 두지 않는다.
- OpenWebUI 채팅 화면은 new-api가 제공하는 링크(버튼)를 거쳐야만 진입할 수 있다.
- 채팅에서 나가는 실제 LLM 호출도 new-api를 경유하며, **사용자별로 과금/사용량이 분리**되어야 한다(공유 시스템 키 사용 금지).
- new-api 관리자 계정은 OpenWebUI 관리자로도 동작해야 한다.

두 코드베이스(OpenWebUI, new-api) 모두 수정 가능하다는 전제 하에 설계했다. new-api 쪽 구현은 별도 문서(`2026-08-16-oauth2-provider-design.md`, new-api 리포지토리)에서 다루며, 본 문서는 **OpenWebUI 쪽에서 구현해야 할 부분**만 다룬다.

## 범위

**포함:**
- new-api가 시작하는(IdP-initiated) OAuth2 인증 코드 교환 콜백 라우트
- 발급받은 access_token을 사용자별로 저장하고, 이후 채팅 요청 시 new-api 게이트웨이 호출에 재사용하는 커넥션 계층 변경
- 로컬 회원가입/로그인/개인정보/개인 API키/사용자관리 UI 제거
- 위 흐름에 필요한 에러 처리, 세션 만료 정책, 설정값

**제외 (본 설계 범위 밖):**
- new-api 쪽 OAuth2 프로바이더 구현 자체 (`/oauth2/authorize`, `/oauth2/token`, `/oauth2/userinfo`, "Open in Chat App" 링크 발급 로직) — new-api 팀 담당
- `is_admin` 클레임이 실제로 `/oauth2/userinfo`에 추가되기 전까지의 관리자 수동 관리 절차는 운영 가이드로만 남기고, 이번 구현에서 UI를 새로 만들지 않음 (기존 관리자 화면 중 역할 변경 기능만 유지)
- new-api를 OpenWebUI의 LLM 커넥션으로 등록하는 것 자체(관리자가 커넥션 URL/타입을 등록하는 일반적인 설정 작업)는 기존 OpenWebUI 기능으로 이미 가능하므로 별도 구현 대상이 아님. 본 설계에서 새로 구현하는 것은 "그 커넥션이 관리자 고정 키가 아니라 세션별 토큰을 쓰게 만드는 것"뿐이다.

## 아키텍처 개요

```
[사용자 브라우저]
     │ 1. new-api.example.com 에서 로그인 (new-api 자체 로그인 방식)
     │ 2. "Open in Chat App" 버튼 클릭 (같은 탭 클릭/JS 네비게이션 — 복사 가능한 URL로 노출하지 않음)
     │    → new-api가 자체 세션으로 인증코드(code) 발급, same-origin에서 곧바로 아래로 이동
     ▼
GET https://<openwebui-host>/auth/newapi/callback?code=...&state=...   ← OpenWebUI 신규 라우트
     │
     │ 3. (서버-서버) POST /oauth2/token       → access_token, expires_in=86400 획득
     │ 4. (서버-서버) GET  /oauth2/userinfo    → {sub, email, name[, is_admin]} 획득
     │ 5. sub 우선/email 차선으로 로컬 계정 조회 또는 생성
     │ 6. access_token + 만료시각을 OAuthSessions(provider='newapi', user_id)에 저장
     │ 7. OpenWebUI 세션 쿠키 발급 — 만료를 24h로 고정 (전역 auth.jwt_expiry와 무관하게 이 흐름에서는 강제)
     ▼
[브라우저를 / 로 리다이렉트 → 채팅 화면]

--- 채팅 중 ---
[커넥션 계층] new-api용 커넥션이 "세션 토큰 모드"인 경우:
     → 현재 요청 user_id로 OAuthSessions에서 저장된 access_token 조회
     → Authorization: Bearer <access_token> 로 new-api 릴레이 호출
       (POST /v1/chat/completions, GET /v1/models 등)
     → 401 수신 시: 배너로 "연결 끊김, 다시 연결하기"(NEWAPI_ENTRY_URL) 안내
```

`state`는 OpenWebUI가 발급한 적이 없으므로 검증하지 않는다(수신만 하고 그대로 무시). 이는 IdP-initiated 플로우의 구조적 특성이며, 잔여 리스크(공격자의 유효한 code를 피해자가 클릭하면 피해자 브라우저가 공격자 소유의 OpenWebUI 계정에 로그인되는 "세션 스와핑")를 의식적으로 수용한다 — 기존 계정 탈취로는 이어지지 않으므로 허용 가능한 트레이드오프로 판단했다.

## 컴포넌트 상세

### 1. 신규 라우트 — `GET /auth/newapi/callback`

- 새 파일 `backend/open_webui/routers/newapi_sso.py`로 분리한다(기존 `auths.py`에 합치지 않음 — 책임이 다르고 이 통합만 별도로 유지보수하기 위함).
- `main.py`에 `/api/v1` 프리픽스 없이 최상위 경로로 등록한다. new-api 쪽에 등록되는 `redirect_uri`와 대소문자·경로가 정확히 일치해야 한다.
- 세션 생성은 기존 `create_session_response`(`backend/open_webui/routers/auths.py:164`)를 재사용하되, 이 흐름에서 발급하는 토큰만 `expires_delta`를 24시간으로 고정 지정한다(전역 `auth.jwt_expiry` 설정값을 참조하지 않고 이 경로에서만 하드코딩 상수를 사용).
- 사용자 매칭은 기존 `Users.get_user_by_oauth_sub` → (`oauth.merge_accounts_by_email` 활성 시) 이메일 매칭 순서를 그대로 재사용한다. 이메일 도메인 제한은 두지 않는다(new-api를 통과한 사용자는 무조건 허용).
- 콜백 처리 중에는 요청에 담긴 기존 쿠키/세션을 절대 신뢰하지 않는다 — 매번 새로 교환한 토큰의 신원 정보만으로 계정을 확정한다(세션 스와핑 잔여 리스크가 기존 로그인 세션을 오염시키지 않도록 하는 안전장치).

### 2. 설정값 (환경변수, 신규)

```
ENABLE_NEWAPI_SSO=true
NEWAPI_OAUTH_BASE_URL=https://<newapi-host>
NEWAPI_OAUTH_CLIENT_ID=<new-api 팀이 발급>
NEWAPI_OAUTH_CLIENT_SECRET=<new-api 팀이 발급>
NEWAPI_ENTRY_URL=<사용자를 new-api 로그인/진입 화면으로 되돌려보낼 URL>
```

기존 설정값은 값만 조정한다:

```
ENABLE_SIGNUP=false
ENABLE_API_KEYS=false
oauth.merge_accounts_by_email=true
auth.jwt_expiry=24h
```

`NEWAPI_OAUTH_BASE_URL`, `NEWAPI_OAUTH_CLIENT_ID`, `NEWAPI_OAUTH_CLIENT_SECRET`, `NEWAPI_ENTRY_URL`의 실제 값은 new-api 운영팀이 제공해야 하며(응답 문서 5절 체크리스트), 배포 전 반드시 채워야 한다. 관리자 설정 화면에는 노출하지 않고 환경변수로만 주입한다(다른 인증 관련 관리자 화면들을 이번에 숨기기로 한 것과 일관됨).

### 3. 데이터 저장

신규 테이블을 만들지 않는다. 기존 `OAuthSessions`(`backend/open_webui/models/oauth_sessions.py`)를 `provider='newapi'`로 재사용한다:
- 저장: `access_token`(Fernet 암호화, 기존 메커니즘 그대로), 발급 시각, `expires_in`(86400초)으로부터 계산한 만료 시각.
- 이 세션은 `OAUTH_PROVIDERS` 설정에 `newapi`가 등록되어 있지 않으므로, 기존 `OAuthManager`의 표준 토큰 리프레시 로직(`backend/open_webui/utils/oauth.py`의 `_refresh_token`)이 이 레코드를 건드리지 않는다 — 우리가 새로 작성하는 콜백/커넥션 코드만 이 레코드를 직접 읽고 쓴다. new-api 쪽에 리프레시 토큰이 없으므로(문서 확인됨), 만료 시 재발급이 아니라 재로그인(new-api로 되돌아가기)만 지원한다.

### 4. 커넥션 계층 변경 (가장 큰 구현 범위)

`backend/open_webui/routers/openai.py`의 커넥션 설정에 인증 모드 필드를 추가한다(예: `auth_mode: "admin_key" | "session_token"`, 기존 커넥션은 기본값 `admin_key`로 하위호환 유지).

- `auth_mode == "session_token"`인 커넥션은 `get_openai_connection`이 관리자 고정 키를 읽는 대신, 현재 요청의 `user_id`로 `OAuthSessions`에서 `provider='newapi'` 토큰을 조회해 `Authorization` 헤더에 사용한다.
- 토큰이 없거나 이미 만료된 경우, 관리자 키로 조용히 폴백하지 않고 명시적으로 실패시켜 프론트엔드가 "다시 연결하기" 배너를 띄우도록 한다.
- `app.state.OPENAI_MODELS` 모델 목록 캐시는 지금까지 커넥션 단위(하나의 고정 키 기준)로 캐싱되어 있어, `session_token` 모드 커넥션에는 이 캐시 전제가 깨진다. 이런 커넥션에 대해서는 캐시를 사용자별 키로 분리하거나, 매 요청마다 새로 조회하도록 캐시 자체를 우회한다.

### 5. 프론트엔드 변경

| 대상 | 처리 |
|---|---|
| 로그인 페이지 | 회원가입/로그인 폼 제거. "new-api에서 로그인해주세요" 안내 문구 + `NEWAPI_ENTRY_URL` 링크 버튼만 남김 |
| 설정 화면 – 비밀번호/계정 | 탭 제거 |
| 설정 화면 – 개인 API 키 | 탭 제거 (백엔드도 `ENABLE_API_KEYS=false`로 차단) |
| 관리자 패널 – 사용자 관리 | 사용자 생성/삭제 UI 제거. `is_admin` 클레임이 아직 없으므로 **역할 변경 기능만 임시로 유지**(추후 클레임 반영되면 이마저 제거 검토) |
| 채팅 화면 | new-api 릴레이 401 발생 시 표시할 "연결 끊김 — 다시 연결하기" 배너 신규 추가 (클릭 시 `NEWAPI_ENTRY_URL`로 이동) |

> **결정**: 위 표는 로그인/회원가입 **폼(UI)** 제거를 다루지만, 백엔드의 `POST /api/v1/auths/signin`(로컬 비밀번호 로그인 API) 자체도 `ENABLE_NEWAPI_SSO=true`일 때 함께 차단한다. 폼만 숨기면 기존 로컬 계정(또는 API를 직접 호출하는 누구든)이 new-api를 거치지 않고 로그인할 수 있어 "완전히 new-api에 종속" 목표와 어긋난다. 관리자 락아웃에 대한 별도의 비상 예외(break-glass) 경로는 만들지 않는다(YAGNI) — 필요 시 `ENABLE_NEWAPI_SSO`를 임시로 끄고 재배포하는 것으로 대응한다.

## 에러 처리 및 엣지 케이스

| 상황 | 사용자 노출 | 로그 레벨 |
|---|---|---|
| `code` 파라미터 누락/형식 오류 | "잘못된 접근입니다. new-api에서 다시 시도해주세요" + `NEWAPI_ENTRY_URL` | WARNING |
| `invalid_grant`(코드 만료/재사용) | 동일 문구 (재시도로 해결됨 — new-api에서 새 code 발급) | INFO |
| `invalid_client` | "일시적인 오류입니다, 잠시 후 다시 시도해주세요" (사용자 재시도로 해결되지 않음을 인지) | **ERROR + 알림** (운영 설정 문제) |
| `/oauth2/token`, `/oauth2/userinfo` 네트워크/타임아웃 오류 | "new-api에 연결할 수 없습니다" + 재진입 링크 (동일 code 재사용하지 않고 항상 새 code로) | ERROR |
| `userinfo` 200이나 `sub`/`email` 누락 | 계정 생성하지 않고 하드 실패, 동일 안내 문구 | ERROR |
| `userinfo` 401 (문서상 이례적 상황) | 동일 안내 문구 | ERROR (invalid_grant와 로그로 구분) |
| 채팅 중 new-api 릴레이 401 (토큰 만료 또는 new-api 로그아웃) | "연결 끊김 — 다시 연결하기" 배너, 다음 메시지 전송 차단 | INFO |

**로그아웃 처리**: new-api 로그아웃 시 access_token이 즉시 무효화되므로, 별도의 로그아웃 웹훅 없이도 다음 채팅 요청이 자동으로 401 → 재연결 배너로 이어진다. "로그아웃 시 채팅 접근 차단" 요구사항이 이 설계만으로 충족된다.

**세션 상한 24h의 근거**: new-api의 기본 세션 토큰 수명은 30일이나, "로그아웃하지 않고 탭만 닫는" 시나리오의 위험 노출 기간을 줄이기 위해 24h로 단축하기로 new-api 팀과 확인했다. 이 상한이 실제로 지켜지려면 OpenWebUI 쪽 세션도 이 24h를 넘거나 활동에 따라 슬라이딩 연장되면 안 된다 — 기존 `create_session_response`는 발급 시점 기준 고정 만료를 계산하는 방식(슬라이딩 갱신 없음)이라 이 요구사항을 이미 만족한다.

**링크 재생/미리보기 위험**: new-api 쪽에서 "Open in Chat App" 링크를 복사 가능한 평문 URL로 노출하지 않도록 이미 요청함(응답 문서 3절). OpenWebUI 쪽에서 추가로 할 수 있는 방어는 없다 — new-api 쪽 구현에 의존.

## new-api 팀 의존성

아래 항목은 OpenWebUI 구현이 진행되기 전에 확정되어야 한다. 상세 내용과 배경은 `2026-08-17-openwebui-response-to-newapi-oauth2-handoff.md` 참고.

- [ ] `client_id`, `client_secret`, new-api 베이스 URL, 등록할 정확한 `redirect_uri` 값 수령
- [ ] code 120초/1회용, access_token 24h + 로그아웃 즉시 무효화, access_token이 릴레이 엔드포인트에서 사용자 본인 크레딧으로 인증됨 — 이 세 가지 전제 재확인
- [ ] (블로킹 아님) `/oauth2/userinfo`에 `is_admin` 클레임 추가
- [ ] "Open in Chat App" 링크를 same-tab 네비게이션으로만 노출(복사 가능한 URL 금지)

## 테스트 전략

**백엔드 단위 테스트** (`/auth/newapi/callback`, new-api 응답 목킹):
- 신규 사용자 생성 / `sub` 매칭 / `email` fallback 매칭 각각 성공 경로
- `invalid_grant`, `invalid_client`, 네트워크 타임아웃, `userinfo` 401, `sub`/`email` 누락 각각 위 표의 사용자 메시지·로그 레벨이 정확히 나오는지
- 동일 `code`로 콜백을 두 번 호출 시 두 번째는 반드시 `invalid_grant` 경로를 타고 계정 중복 생성/세션 덮어쓰기가 없는지
- 콜백이 요청에 담긴 기존 쿠키를 무시하고 교환된 토큰만으로 신원을 확정하는지(세션 스와핑이 계정 침해로 확산되지 않음을 보장하는 회귀 테스트)
- 발급 세션이 전역 `auth.jwt_expiry`와 무관하게 항상 24h로 고정되는지, 그리고 **활동이 계속 있어도 슬라이딩 갱신 없이 발급 시점 기준 24h 후 무조건 만료**되는지

**커넥션 계층 테스트**:
- `session_token` 모드 커넥션이 호출 사용자 본인의 `OAuthSessions` 토큰만 사용하는지 (동시 요청 두 사용자로 검증, 토큰 혼선 없는지)
- 토큰 없음/만료 시 관리자 키로 폴백하지 않고 명시적 오류로 이어지는지
- 모델 목록 캐시가 사용자 간에 새지 않는지

**E2E 테스트** (Playwright, 기존 `docker-compose.playwright.yaml` 스택 재사용):
- 로그인 화면 → new-api 진입 링크(테스트 스텁 서버) → 콜백 → 채팅 화면 도달 → 메시지 전송 성공까지 전체 플로우
- 스텁 서버가 401을 반환하도록 전환 → "다시 연결하기" 배너가 뜨고 전송이 막히는지

**자동화 불가 영역 (new-api 팀과 조율 필요)**:
- 실제 new-api 스테이징 환경 대상 스모크 테스트 — 목 기반 테스트는 문서 스키마를 기준으로 하므로, new-api 실제 응답이 스키마와 어긋나면(드리프트) 우리 테스트는 통과해도 실제 연동은 깨질 수 있다. 배포 전 최소 1회 실제 인스턴스 확인 필수.
- `redirect_uri` 정확 일치 여부는 new-api 운영 설정에 달려있어 자동화 테스트로 검증 불가.
- 메신저 링크 미리보기(언퍼링) 봇에 의한 code 소모 리스크는 new-api의 링크 노출 방식에 달려있어 OpenWebUI 쪽 테스트로 검증 불가.

## 추후 논의 사항

- `is_admin` 클레임이 반영되면, 관리자 패널에 임시로 남겨둔 "역할 변경" 기능도 제거할지 여부를 재검토한다.
- 관리자가 커넥션 설정 화면에서 `auth_mode`를 직접 편집할 수 있게 UI를 노출할지, 아니면 이번처럼 환경변수/코드 레벨로만 고정할지는 이번 구현 범위에서는 코드 레벨 고정으로 가고, 필요성이 생기면 별도로 다룬다.
