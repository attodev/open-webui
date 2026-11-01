# Open WebUI ↔ new-api ↔ RAG MCP 전체 스택 (Docker)

new-api, Open WebUI, RAG MCP 서버를 **서로 연동된 상태로** 띄우는 배포 구성입니다. new-api 저장소의 `deploy/openwebui-test`(커밋 `bf236c829`)를 바탕으로 했고, new-api 쪽 파일은 수정하지 않았습니다.

외부 MCP 클라이언트(Claude Code, Claude Desktop, Cursor 등)에서 **new-api API 키 하나로** Open WebUI 지식 베이스를 검색할 수 있게 하는 것이 목표입니다.

- **`./bootstrap.sh`가 하는 일:** 연동에 필요한 계정, 키, 설정을 만들고 전체를 띄우는 데까지입니다.
- **사람이 할 일:** 모델·임베딩 채널, 사용자, 지식 베이스는 직접 준비합니다(5절).

## 0. 구성과 흐름

| 서비스 | 이미지 (`.env`) | 역할 |
| --- | --- | --- |
| `postgres`, `redis` | `POSTGRES_IMAGE`, `REDIS_IMAGE` | new-api 저장소 |
| `new-api` | `NEW_API_IMAGE` | 모델 중계, OIDC 제공자(Open WebUI 로그인), `/oauth2/token-from-key` |
| `open-webui` | `OPEN_WEBUI_IMAGE` | 채팅, 지식 베이스(RAG), OAuth 토큰 교환 |
| `mcp-rag` | `MCP_IMAGE` | **new-api 키 인증 MCP 서버** (포트 `MCP_PORT`) |
| `mcp-rag-owui-key` (선택) | `MCP_OWUI_KEY_IMAGE` | Open WebUI API 키 인증 MCP 서버. `OWUI_ENABLE_API_KEYS=true`일 때만 띄웁니다 |

**서로 주고받는 값**

`bootstrap.sh`가 비어 있는 값을 만들어 `.env`와 new-api 설정 양쪽에 넣습니다.

| 값 | new-api 쪽 | Open WebUI·MCP 쪽 |
| --- | --- | --- |
| `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET` | `oauth2.client_id`, `oauth2.client_secret` | `OAUTH_CLIENT_ID`, `OAUTH_CLIENT_SECRET` (웹 로그인, introspection) |
| issuer 주소 `http://PUBLIC_HOST:NEW_API_PORT` | `ServerAddress` | `OPENID_PROVIDER_URL` |
| 로그인 콜백, 로그아웃 주소 | `oauth2.redirect_uri`, `oauth2.app_url`, `oauth2.post_logout_redirect_uri` | `OPENID_REDIRECT_URI`, `WEBUI_URL` |
| `OWUI_SERVICE_KEY` (new-api 서비스 토큰) | `openwebui.service_api_key` | `OPENAI_API_KEYS`, `RAG_OPENAI_API_KEY` 등 |
| `OWUI_JWT_SECRET` | `openwebui.jwt_secret` | `FORWARD_USER_INFO_HEADER_JWT_SECRET` |
| `OIDC_MCP_CLIENT_ID` (`openwebui-mcp`) | `oauth2.token_from_key_client_id`, `token_from_key_enabled` | `OAUTH_TOKEN_EXCHANGE_TRUSTED_CLIENT_IDS` |

**MCP 검색 한 번의 흐름**

```
MCP 클라이언트 ──Bearer sk-(사용자의 new-api 키)──▶ mcp-rag
mcp-rag ──POST /oauth2/token-from-key──▶ new-api                   → 액세스 토큰 (10분)
mcp-rag ──POST /api/v1/auths/oauth/oidc/token/exchange──▶ open-webui
open-webui ──userinfo / introspect──▶ new-api   (PUBLIC_HOST 주소로 검증)
open-webui ──▶ mcp-rag : Open WebUI JWT (최대 10분 캐시)
mcp-rag ──JWT──▶ open-webui /api/v1/knowledge, /api/v1/retrieval/query/collection
```

**파일**

| 파일 | 용도 |
| --- | --- |
| `bootstrap.sh` | 처음 연동합니다. 비밀값을 만들고, new-api를 설정하고, 전체를 띄운 뒤 연동을 확인합니다. 여러 번 실행해도 안전합니다. |
| `docker-compose.yml` | 전체 스택입니다. **빌드하지 않고** 이미지만 씁니다. 재시작도 이 파일로 합니다. |
| `.env.example` | 설정 견본입니다. 필수 항목은 `PUBLIC_HOST`와 이미지 태그뿐입니다. |
| `smoke_mcp.py` | MCP 동작 확인 스크립트로, MCP 컨테이너 안에서 실행합니다. |

> new-api 단독 스택(`deploy/openwebui-test`)과 **동시에 띄우지 마세요.** 기본 포트 3000과 8080이 겹칩니다. compose 프로젝트 이름이 다르므로(`owui-rag-stack`) 데이터는 섞이지 않습니다.

## 1. 이미지 준비

`bootstrap.sh`는 이미지를 빌드하거나 내려받지 않습니다. 처음에 **로컬에 있는지만 확인**하고, 없으면 그 이름을 알려 주고 멈춥니다.

| 이미지 | 준비 방법 |
| --- | --- |
| new-api | new-api 저장소 `OpenWebUI` 브랜치, `bf236c829` 이후. 방법은 new-api의 `deploy/openwebui-test/README.md` 1절을 따르고, 예시는 아래에 있습니다. |
| Open WebUI | `docker pull ghcr.io/open-webui/open-webui:main` (가능하면 태그를 고정) |
| MCP 서버 | 이 저장소 루트에서 `docker build -t openwebui-rag-mcp-newapi:latest mcp-servers/openwebui-rag-newapi` |
| (선택) Open WebUI 키 방식 MCP | `docker build -t openwebui-rag-mcp:latest mcp-servers/openwebui-rag` |
| PostgreSQL, Redis | `docker pull postgres:15-alpine redis:7-alpine` |

new-api 이미지 빌드 예시:

```bash
cd <new-api 저장소> && git switch OpenWebUI
docker build -t new-api-owui:$(git rev-parse --short HEAD) .
```

인터넷이 안 되는 머신으로 옮길 때는 `docker save`와 `docker load`를 씁니다.

```bash
# 인터넷이 되는 머신에서
docker save new-api-owui:<tag> ghcr.io/open-webui/open-webui:main openwebui-rag-mcp-newapi:latest \
  postgres:15-alpine redis:7-alpine | gzip > owui-rag-images.tar.gz
# 대상 머신에서
docker load < owui-rag-images.tar.gz
```

만든 태그는 `.env`의 `*_IMAGE` 항목에 맞춥니다.

## 2. 호스트 이름 정하기 (가장 중요)

브라우저와 Open WebUI 컨테이너가 new-api를 **같은 주소**(`http://PUBLIC_HOST:NEW_API_PORT`)로 볼 수 있어야 합니다. 이 주소가 OIDC issuer가 되고, 토큰 검증도 이 주소로 합니다. `localhost`는 쓸 수 없습니다.

| 상황 | `PUBLIC_HOST` | 추가 설정 |
| --- | --- | --- |
| 같은 LAN, 공개 DNS 사용 가능 | `192-168-0-10.nip.io` (호스트 IP를 `-`로 이어 씀) | 없음 |
| 사내 DNS 이름이 있음 | 그 이름 | 없음 |
| 오프라인 | `newapi.local` 등 | 브라우저 머신의 `/etc/hosts`에 추가하고, `open-webui` 서비스의 `extra_hosts` 주석을 해제 |

MCP 서버는 new-api와 Open WebUI를 컨테이너 내부 이름으로 부르므로 이 설정과 직접 관련은 없습니다. 하지만 Open WebUI가 이 주소로 토큰을 검증하므로, 여기가 틀리면 MCP도 동작하지 않습니다.

## 3. 처음 연동: `./bootstrap.sh`

```bash
cd deploy/openwebui-rag
cp .env.example .env
# .env 편집: PUBLIC_HOST, *_IMAGE 태그, (필요하면) 포트, NEW_API_ROOT_EMAIL, MCP_BIND
./bootstrap.sh
```

| 단계 | 하는 일 |
| --- | --- |
| 1 | `.env`에 적힌 이미지가 로컬에 있는지 확인합니다. |
| 2 | **비어 있는 비밀값과 비밀번호를 무작위로 만들어 `.env`에 기록**합니다. 대상은 `PG_PASSWORD`, `REDIS_PASSWORD`, `NEW_API_SESSION_SECRET`, `NEW_API_CRYPTO_SECRET`, `OIDC_CLIENT_SECRET`, `OWUI_JWT_SECRET`, `OWUI_SECRET_KEY`, `NEW_API_ROOT_PASSWORD`, `NEW_API_SERVICE_PASSWORD`입니다. 이미 값이 있으면 그대로 씁니다. |
| 3 | PostgreSQL, Redis, new-api를 띄웁니다. |
| 4 | new-api를 설정합니다. 아래 표를 보세요. |
| 5 | Open WebUI와 MCP 서버를 띄우고 healthy가 될 때까지 기다립니다. |
| 6 | 연동을 확인합니다. 아래 표를 보세요. |

**단계 4: new-api 설정**

| 항목 | 내용 |
| --- | --- |
| 관리자 | 루트 계정을 만들고, `NEW_API_ROOT_EMAIL`이 있으면 이메일도 넣습니다. |
| 서비스 계정과 토큰 | `owui-svc` 계정과 `openwebui-service` 토큰(쿼터 무제한, 만료 없음)을 만들고, 키를 `.env`의 `OWUI_SERVICE_KEY`에 적습니다. |
| OIDC 제공자 | issuer, 클라이언트, 콜백, 로그아웃 주소를 설정합니다. |
| relay | 서비스 키와 JWT 비밀을 설정합니다. |
| API 키로 토큰 발급 | 기능을 켜고, client_id를 `openwebui-mcp`로 설정합니다. |

**단계 6: 연동 확인**
- Open WebUI 컨테이너가 new-api discovery에 닿는지
- Open WebUI 로그인이 new-api로 리다이렉트되는지
- 토큰 교환과 token-from-key가 켜져 있는지
- MCP 서버가 잘못된 키를 new-api에서 정상적으로 거부하는지

끝에 접속 주소가 출력됩니다. 루트 비밀번호를 자동으로 만들었다면 그 비밀번호도 출력되며, `.env`에도 저장됩니다.

**만들지 않는 것**
- 모델·임베딩 채널
- 일반 사용자
- 지식 베이스
- MCP 테스트용 키

이것들은 5절에서 직접 준비합니다.

**주의**
- 한 번 띄운 뒤에는 `.env`의 비밀값(`PG_PASSWORD`, `*_SECRET`, `NEW_API_ROOT_PASSWORD` 등)을 **바꾸지 마세요.** DB와 세션이 처음 값에 묶여 있습니다.
- 다시 처음부터 시작하려면 `docker compose down -v`로 데이터까지 지운 뒤 실행합니다.

## 4. 재시작·운영

```bash
docker compose up -d       # 기동·재시작, .env 변경 반영
docker compose ps
docker compose down        # 중지 (데이터 볼륨 유지. down -v 는 데이터까지 삭제)
```

`OWUI_ENABLE_API_KEYS=true`라면 위 명령에 `--profile owui-key`를 붙입니다.

연동이 끝난 뒤에는 `bootstrap.sh`를 다시 돌릴 필요가 없습니다. 다만 issuer 주소(`PUBLIC_HOST`, 포트)나 client_id처럼 **new-api 쪽 설정에 들어가는 값**을 바꿨다면 `./bootstrap.sh`를 다시 실행하세요.

**Open WebUI 설정은 환경 변수가 기준입니다.** Open WebUI는 기본적으로 처음 뜰 때 받은 환경 변수를 DB에 저장하고, 그 뒤로는 DB 값을 씁니다. 이 스택은 `ENABLE_PERSISTENT_CONFIG=false`로 두어 **재시작할 때마다 환경 변수를 다시 읽습니다.** 그래서 `.env`를 고치고 `docker compose up -d`를 하면 바로 반영됩니다.

**대가:** 관리자 화면에서 바꾼 설정은 재시작하면 환경 변수 값으로 돌아갑니다. 계속 쓸 설정은 `docker-compose.yml`이나 `.env`에서 바꾸세요. 지식 베이스, 문서, 채팅, 사용자는 DB에 저장되므로 해당하지 않습니다.

new-api 단독 배포에서 손으로 하던 Open WebUI 설정은 아래 환경 변수로 들어가 있습니다.

| 원래 수동 설정 | 이 스택의 환경 변수 |
| --- | --- |
| 첫 로그인 사용자를 관리자로 | `ENABLE_OAUTH_ROLE_MANAGEMENT=true`: new-api 관리자·루트는 로그인할 때마다 `admin`, 나머지는 `user` |
| `X-Task` 헤더 | `OPENAI_API_CONFIGS={"0":{"enable":true,"headers":{"X-Task":"{{TASK}}"}}}` |
| 모델 공개 범위 Public | `BYPASS_MODEL_ACCESS_CONTROL=true` (`OWUI_BYPASS_MODEL_ACCESS_CONTROL`) |
| 일반 사용자의 지식 베이스 만들기 권한 | `USER_PERMISSIONS_WORKSPACE_KNOWLEDGE_ACCESS=true` (`OWUI_USER_KNOWLEDGE_ACCESS`). Open WebUI 기본값은 관리자만입니다 |

## 5. 실제 테스트를 위한 준비 (직접 할 일)

### 5-1. new-api 채널 추가

new-api 콘솔(`http://PUBLIC_HOST:NEW_API_PORT`)에 루트로 로그인해 **채널**을 추가합니다.

1. **채팅 모델 채널:** 쓸 모델(예: `gpt-4o-mini`)을 서비스하는 채널입니다.
2. **임베딩 채널:** `.env`의 `OWUI_EMBEDDING_MODEL`(기본 `text-embedding-3-small`)과 **정확히 같은 이름**의 모델을 서비스해야 합니다. 이것이 없으면 지식 베이스에 문서를 올릴 때 색인이 실패합니다.
   - 다른 임베딩 모델을 쓰려면 `.env`의 `OWUI_EMBEDDING_MODEL`을 바꾸고 `docker compose up -d`를 합니다.
   - 임베딩 모델을 바꾸면 **기존 지식 베이스는 다시 색인**해야 합니다. 관리자 → Settings → Documents에서 합니다.
3. **가격 비율:** new-api에 기본 가격이 없는 모델 이름이면 설정 → 운영 설정 → 모델 비율에 가격을 넣어야 호출이 됩니다.
4. 채팅·임베딩 호출은 Open WebUI 서비스 계정(`owui-svc`)과 relay로 연결된 각 사용자에게 청구됩니다.

### 5-2. 사용자 준비

**MCP를 쓸 사용자의 조건**
- (a) new-api 계정에 **이메일**이 있어야 합니다.
- (b) Open WebUI에 **웹으로 한 번 로그인**해 계정이 만들어져 있어야 합니다.

**new-api 사용자 만들기:** 방법은 셋 중 하나입니다.
- 이메일 인증 가입(SMTP 설정 필요)
- 사용자가 직접 가입한 뒤 프로필에서 이메일 등록
- 관리자 화면에서 만든 뒤 DB로 이메일 넣기. 관리자 "사용자 추가"는 이메일을 저장하지 않으므로 아래처럼 넣습니다.

  ```bash
  docker compose exec -T postgres psql -U newapi -d newapi -c "update users set email='alice@example.com' where username='alice';"
  ```

**첫 로그인:** 그 사용자가 브라우저로 `http://PUBLIC_HOST:OPEN_WEBUI_PORT`를 엽니다. new-api 로그인 화면이 나오고, 로그인하면 Open WebUI로 돌아와 계정이 만들어집니다.

**루트 계정:** 루트도 `NEW_API_ROOT_EMAIL`이 있으면 같은 방법으로 로그인할 수 있고, Open WebUI 관리자가 됩니다. 다만 관리자·루트는 relay 자동 연결 대상이 아닙니다. 그래서 Open WebUI 채팅 화면에 모델이 보이지 않을 수 있습니다. 이때는 new-api 사용자 편집 → "Open WebUI 연결"에서 수동으로 연결합니다(new-api README 6절). MCP 검색에는 relay 연결이 필요 없습니다.

### 5-3. MCP용 new-api 키 만들기

1. 사용자가 new-api 콘솔 → **토큰**에서 키를 하나 만듭니다(예: 이름 `mcp`).
2. 이 키는 신원 확인용이라 쿼터를 보지 않습니다.
3. **IP 제한**을 건다면 MCP 서버의 IP를 허용해야 합니다(7절).

### 5-4. 지식 베이스 만들기

1. 사용자가 Open WebUI → **워크스페이스 → 지식**에서 지식 베이스를 만들고 문서를 올립니다.
2. 색인이 끝나면 문서 목록에 나타납니다. 실패하면 5-1의 임베딩 채널을 확인합니다.
3. 다른 사용자도 검색하게 하려면 접근 범위를 그 사용자나 그룹, 또는 Public으로 엽니다.

**MCP 검색 결과는 그 사용자가 Open WebUI 화면에서 볼 수 있는 지식과 똑같습니다.**

### 5-5. MCP 동작 확인

```bash
KEY=sk-...   # 5-3에서 만든 키
docker compose exec mcp-rag python /smoke_mcp.py "$KEY" "검색어"
```

정상이면 다음이 출력되고, 종료 코드는 0입니다.
- `tools: list_knowledge_bases, search_knowledge`
- 지식 베이스 목록
- 검색 결과(`[1] 파일명 (score …)` 형식)

**토큰 흐름만 따로 확인하려면 (curl):**

```bash
AT=$(curl -s -X POST -H "Authorization: Bearer $KEY" http://PUBLIC_HOST:NEW_API_PORT/oauth2/token-from-key | jq -r .access_token)
curl -s -H "Authorization: Bearer $AT" http://PUBLIC_HOST:NEW_API_PORT/oauth2/userinfo
curl -s -X POST -H 'Content-Type: application/json' -d "{\"token\":\"$AT\"}" \
  http://PUBLIC_HOST:OPEN_WEBUI_PORT/api/v1/auths/oauth/oidc/token/exchange | jq '{token: (.token|.[0:20]), expires_at}'
```

### 5-6. 확인 표

| 확인 | 기대 결과 |
| --- | --- |
| 5-5 스모크 테스트 | 목록과 검색이 성공함 |
| 없는 키 / 비활성 키 | `newapi API 키가 유효하지 않습니다` |
| Open WebUI에 로그인한 적 없는 사용자의 키 | `Open WebUI에 웹으로 한 번 로그인한 뒤 다시 시도하세요` |
| 다른 사용자에게만 공개한 지식 베이스 | 목록에 나오지 않음 |
| new-api에서 그 키를 비활성화 | 늦어도 약 10분(토큰 수명) 안에 검색이 거부됨 |
| new-api 감사 로그 | 사용자 로그에 `oidc token-from-key issued token_id=… client_id=openwebui-mcp` 기록 |

로그 확인:
- `docker compose logs -f mcp-rag`
- `docker compose logs -f new-api | grep -i oidc`
- `docker compose logs -f open-webui`

## 6. MCP 클라이언트 등록

각 사용자는 **자기 new-api 키**를 씁니다. 주소는 `http://<MCP 서버 호스트>:MCP_PORT/mcp`입니다.

**다른 PC에서 쓰려면** `.env`에서 `MCP_BIND=0.0.0.0`으로 바꾸고 `docker compose up -d`를 합니다. 이 서버에는 HTTPS와 자체 접근 제어가 없으므로 **사내망 안에서만** 여세요.

**Claude Code**

```bash
claude mcp add --transport http openwebui-rag http://PUBLIC_HOST:8000/mcp --header "Authorization: Bearer sk-..."
```

**Cursor** (`~/.cursor/mcp.json` 또는 프로젝트의 `.cursor/mcp.json`)

```json
{
  "mcpServers": {
    "openwebui-rag": {
      "url": "http://PUBLIC_HOST:8000/mcp",
      "headers": { "Authorization": "Bearer sk-..." }
    }
  }
}
```

**Claude Desktop** (`claude_desktop_config.json`): 설정 파일에는 stdio 서버만 넣을 수 있으므로 `mcp-remote`로 연결합니다. Node.js가 필요합니다.

```json
{
  "mcpServers": {
    "openwebui-rag": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "http://PUBLIC_HOST:8000/mcp", "--allow-http",
               "--header", "Authorization:Bearer ${NEWAPI_KEY}"],
      "env": { "NEWAPI_KEY": "sk-..." }
    }
  }
}
```

## 7. 운영 시 알아둘 점

- **키 하나가 곧 Open WebUI 검색 권한입니다.**
  - new-api 키가 새면 그 사용자의 Open WebUI 지식도 읽힐 수 있습니다.
  - 키를 비활성화하면 새 토큰 발급은 즉시 막힙니다. 다만 이미 발급된 토큰과 MCP 캐시 때문에 **최대 토큰 수명(기본 600초, 최대 900초)까지는** 검색이 될 수 있습니다.
- **new-api가 보는 호출 IP는 MCP 서버입니다.**
  - **키의 IP 제한:** 키에 IP 제한(AllowIps)을 걸었다면 MCP 서버의 IP를 허용해야 합니다. 이 스택에서는 compose 네트워크의 `mcp-rag` 컨테이너 주소입니다.
  - **IP당 호출 제한:** new-api의 IP당 분당 60회 제한도 MCP 서버 하나에 모든 사용자가 몰립니다. 캐시 덕분에 사용자당 약 10분에 1회만 호출하므로 동시 사용자 수백 명까지는 문제없습니다.
- **Open WebUI의 `OAUTH_TOKEN_EXCHANGE_RATE_LIMIT`은 켜지 마세요.** IP 기준이라 같은 문제가 생깁니다. compose에서도 설정하지 않았습니다.
- **허용 클라이언트:** `OAUTH_TOKEN_EXCHANGE_TRUSTED_CLIENT_IDS=openwebui-mcp`이므로 new-api가 MCP용으로 발급한 토큰만 교환됩니다. Open WebUI는 new-api의 introspection으로 이를 확인합니다.
- **Open WebUI 가입 경로:** `ENABLE_SIGNUP=false`라서 Open WebUI 계정은 new-api OIDC로만 만들어집니다. relay의 이메일 자동 연결이 이것을 전제로 하므로 켜지 마세요.

## 8. 문제 해결

new-api와 Open WebUI 로그인·relay 자체의 문제는 new-api README 7절을 보세요.

| 증상 / 메시지 | 원인과 조치 |
| --- | --- |
| bootstrap: `로컬에 없는 이미지` | 1절대로 이미지를 준비하고, `.env`의 태그와 맞춥니다 |
| bootstrap: `루트 로그인 실패` | 이미 초기화된 new-api인데 `.env`의 `NEW_API_ROOT_PASSWORD`가 그때 값과 다릅니다. 원래 값으로 되돌리거나 `docker compose down -v` 후 처음부터 실행합니다 |
| bootstrap: `Open WebUI 컨테이너에서 … 에 닿지 않습니다` | 2절의 호스트 이름 문제입니다 |
| `newapi 관리자가 'API 키로 토큰 발급' 기능을 켜야 합니다` | `./bootstrap.sh`를 다시 실행합니다 |
| `newapi의 OIDC 제공자가 꺼져 있거나 …` | new-api 이미지가 `bf236c829` 이전 버전이거나 `oauth2.enabled`가 꺼져 있습니다 |
| `newapi 계정에 이메일이 없습니다…` | 5-2의 이메일 등록 |
| `Open WebUI에 웹으로 한 번 로그인한 뒤 다시 시도하세요` | 5-2의 첫 로그인 |
| `허용되지 않은 클라이언트이거나 이메일 도메인입니다…` | new-api `oauth2.token_from_key_client_id`와 Open WebUI `OAUTH_TOKEN_EXCHANGE_TRUSTED_CLIENT_IDS`가 다르거나, introspection이 실패했습니다(`OIDC_CLIENT_SECRET` 불일치). `./bootstrap.sh`를 다시 실행합니다 |
| `newapi 토큰이 Open WebUI에서 거부되었습니다…` | Open WebUI가 userinfo나 introspection을 부르지 못했습니다(2절) |
| `IP 제한에 MCP 서버 IP가 허용되어 있지 않습니다` | 키의 IP 제한에 MCP 서버 IP를 추가합니다(7절) |
| `Open WebUI 계정이 승인 대기 중이거나 MFA가 필요해…` | Admin Panel → Users에서 역할이 `pending`인지 확인합니다 |
| `인증 서버에 연결할 수 없습니다` | `docker compose ps`로 `new-api`와 `open-webui` 상태를 확인합니다 |
| 지식 베이스에 문서를 올리면 색인 실패 | 5-1의 임베딩 채널(모델 이름이 `OWUI_EMBEDDING_MODEL`과 같은지)을 확인합니다. `docker compose logs open-webui`에서 embedding 오류를 봅니다 |
| 일반 사용자가 지식 메뉴를 못 씀 | `OWUI_USER_KNOWLEDGE_ACCESS=true`인지 확인한 뒤 `docker compose up -d` |
| 다른 PC에서 MCP 접속 불가 | `MCP_BIND=127.0.0.1`(기본값)이면 이 호스트에서만 접속됩니다(6절) |
