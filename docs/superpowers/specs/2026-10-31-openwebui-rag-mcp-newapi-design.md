# Open WebUI RAG MCP 서버 (newapi 키 인증판) 설계

## 목적과 이유

기존 MCP 서버(`mcp-servers/openwebui-rag/`, 설계: `2026-10-31-openwebui-rag-mcp-design.md`)는 사용자가 Open WebUI API 키를 직접 만들어 등록해야 한다. 사용자가 키를 관리하는 부담을 없애기 위해, **이미 쓰고 있는 newapi API 키 하나만으로** 인증되는 판을 새로 만든다. 사용자 요청: "기존 것은 그대로 두고 이 방향으로 동작하는 걸 새로 만들어줘".

## 목표

- 사용자는 newapi API 키만 등록한다. Open WebUI 키나 이메일은 등록하지 않는다.
- 검색 기능, 도구, 결과 형식, 실행 방식(stdio, HTTP, Docker)은 기존 서버와 같다.
- 권한 판단은 계속 Open WebUI가 사용자 기준으로 한다.

## 비목표

- 기존 `mcp-servers/openwebui-rag/`를 수정하지 않는다.
- Open WebUI 코드를 수정하지 않는다. 환경 변수만 켠다.
- newapi 쪽 구현은 하지 않는다. 별도 창에서 요구서로 진행한다: `2026-10-31-newapi-key-to-oidc-token-requirements.md`.

## 피해야 할 결과

- MCP 서버가 관리자 키처럼 사용자 권한을 넘어서는 비밀을 갖는 것.
- Open WebUI의 신뢰 헤더 로그인처럼 인스턴스 전체의 인증 방식을 바꾸는 것.
- newapi 키 원문이나 발급받은 토큰이 로그에 남는 것.

## 인증 흐름 (확정)

1. 사용자의 newapi 키를 얻는다. stdio 모드에서는 환경 변수 `NEWAPI_API_KEY`에서, HTTP 모드에서는 요청 헤더 `Authorization: Bearer sk-…`에서 가져온다. HTTP 모드에는 환경 변수 대체 경로가 없다(기존 판과 같은 규칙).
2. `POST {NEWAPI_URL}{NEWAPI_TOKEN_PATH}`에 `Authorization: Bearer <키>`를 붙여 보내고, 응답으로 `{access_token, expires_in}`을 받는다(요구서 R1).
3. `POST {OPENWEBUI_URL}/api/v1/auths/oauth/{OPENWEBUI_OAUTH_PROVIDER}/token/exchange`에 본문 `{"token": access_token}`을 보내고, 응답으로 Open WebUI JWT(`token`)와 만료 시각(`expires_at`, 있으면)을 받는다.
4. 이 JWT로 기존과 같은 Open WebUI API를 호출한다(`/api/v1/knowledge/`, `/api/v1/retrieval/query/collection`).

## 내가 정한 사항 (검토 필요)

- **위치와 구성:** `mcp-servers/openwebui-rag-newapi/`에 독립 패키지(`openwebui_rag_mcp_newapi`)로 둔다. 기존 패키지의 클라이언트와 서버 코드를 복사해서 출발한다. 기존 것을 건드리지 않는다는 요구를 지키기 위해 공용 라이브러리로 묶지 않는다.
- **환경 변수:**
  - `OPENWEBUI_URL` (필수)
  - `NEWAPI_URL` (필수)
  - `NEWAPI_TOKEN_PATH` (기본 `/api/oidc/token-from-key`)
  - `OPENWEBUI_OAUTH_PROVIDER` (기본 `oidc`)
  - `NEWAPI_API_KEY` (stdio 모드 전용)
  - `MAX_COLLECTIONS` (기본 20)
  - `MCP_TRANSPORT`, `MCP_HOST`, `MCP_PORT` (기존과 같음)
- **JWT 캐시:**
  - 메모리 캐시이며, 키는 newapi 키 원문의 SHA-256 해시다(원문은 저장하지 않는다).
  - 만료 시각은 Open WebUI 응답의 `expires_at`을 쓴다. 없으면 newapi 토큰의 `expires_in`을 기준으로 잡는다.
  - 만료 60초 전부터는 새로 교환한다.
  - Open WebUI가 401을 돌려주면 캐시를 지우고 한 번만 다시 교환해 재시도한다.
  - 같은 키로 동시에 요청이 와도 교환은 한 번만 일어나게 키별 잠금을 둔다.
  - 캐시 항목은 최대 1000개로 하고, 넘치면 가장 오래된 것부터 지운다.
- **오류 메시지 (사람이 읽을 수 있게):**
  - newapi 401 → "newapi API 키가 유효하지 않습니다"
  - newapi 403 → "newapi 계정이 비활성화되었습니다"
  - newapi 429 → "잠시 후 다시 시도하세요"
  - 그 밖의 newapi 오류 → "newapi 인증 서버 오류"
  - 토큰 교환 403 "Token exchange is disabled" → "Open WebUI 관리자가 ENABLE_OAUTH_TOKEN_EXCHANGE를 켜야 합니다"
  - 토큰 교환 403 "User not found" → "Open WebUI에 웹으로 한 번 로그인한 뒤 다시 시도하세요"
  - 토큰 교환 404 → "OPENWEBUI_OAUTH_PROVIDER 값을 확인하세요"
- **토큰 교환이 200인데 `token`이 없을 때:** 승인 대기 중인 사용자(`next_step: pending`)이거나 MFA 확인 단계에 걸린 경우다. 이때는 캐시하지 않고 오류를 돌려준다. 메시지는 "Open WebUI 계정이 승인 대기 중이거나 MFA가 필요해 외부 인증을 할 수 없습니다. 관리자에게 문의하세요"이다.
- **`expires_at`이 없을 때 (JWT 무기한 설정):** 캐시 수명을 1시간으로 잡는다. newapi 토큰 수명(10분)을 따르면 쓸데없이 자주 교환하게 된다.
- **추가 오류 매핑:** 토큰 교환 403은 `detail` 문구로 구분한다.
  - 400 → "newapi 토큰이 Open WebUI에서 거부되었습니다 (sub/email 누락 또는 검증 실패). newapi 설정을 확인하세요"
  - 403 `ACCESS_PROHIBITED` → "허용되지 않은 클라이언트이거나 이메일 도메인입니다 (OAUTH_TOKEN_EXCHANGE_TRUSTED_CLIENT_IDS / 허용 도메인 확인)"
  - 429 → "Open WebUI 토큰 교환 호출 제한에 걸렸습니다. 잠시 후 다시 시도하세요"
- **호출 횟수 제한 주의:** Open WebUI의 토큰 교환 제한은 호출한 IP 기준이다. 모든 사용자가 MCP 서버의 IP 하나를 공유하므로, README에 `OAUTH_TOKEN_EXCHANGE_RATE_LIMIT`을 켜지 말거나 넉넉하게 잡으라고 안내한다. 캐시 덕분에 교환 횟수는 사용자당 JWT 수명마다 1회 수준이다.
- **provider 이름:** Open WebUI에 등록된 newapi OIDC provider 이름을 `OPENWEBUI_OAUTH_PROVIDER`로 받는다. 일반 OIDC 설정(`OPENID_PROVIDER_URL`)이면 `oidc`이다. 계정은 provider 이름과 `sub`의 조합으로 찾기 때문에 반드시 일치해야 한다.
- **Open WebUI 관리자 사전 설정 (README에 기재):**
  - `ENABLE_OAUTH_TOKEN_EXCHANGE=true`
  - 권장: `OAUTH_TOKEN_EXCHANGE_TRUSTED_CLIENT_IDS=<newapi가 정한 client_id>` (newapi가 introspection을 지원할 때)
  - 필요하면 `OAUTH_TOKEN_EXCHANGE_RATE_LIMIT`
- **테스트:**
  - 단위 테스트는 respx로 newapi와 Open WebUI를 모킹한다. 대상은 캐시 재사용, 만료 후 재교환, 401 재시도, 오류 메시지 매핑, 동시 요청 시 교환 1회.
  - 통합 확인은 가짜 newapi와 가짜 Open WebUI를 띄워 컨테이너 HTTP 모드에서 끝까지 확인한다.
  - 실제 newapi 구현이 끝나면 실제 환경에서 수동으로 확인한다.
- **README:** 한국어로 쓴다. 기존 판과의 차이(newapi 키만 등록), 관리자 사전 설정, 클라이언트 등록 예시, Docker/compose 예시를 넣는다.

## 구현하는 사람이 정할 사항

- 캐시 구현 세부(자료구조, 잠금 방식)
- 기존 코드에서 무엇을 그대로 복사하고 무엇을 다듬을지
