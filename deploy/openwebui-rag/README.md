# Open WebUI ↔ new-api ↔ RAG MCP 전체 스택 (Docker)

new-api 저장소의 `deploy/openwebui-test`(커밋 `bf236c829`) 스택에 **RAG MCP 서버**를 더한 배포 구성입니다. new-api 쪽 파일은 수정하지 않았습니다. `bootstrap.sh`와 `mock_upstream.py`는 그 폴더에서 그대로 복사해 왔습니다.

외부 MCP 클라이언트(Claude Code, Claude Desktop, Cursor 등)에서 **new-api API 키 하나로** Open WebUI 지식 베이스를 검색할 수 있게 하는 것이 목표입니다.

## 0. 구성과 흐름

| 서비스 | 이미지 | 역할 |
| --- | --- | --- |
| `postgres`, `redis` | 공식 이미지 | new-api 저장소 |
| `new-api` | new-api 저장소에서 빌드 (`NEW_API_IMAGE`) | 모델 중계, OIDC 제공자, `/oauth2/token-from-key` |
| `open-webui` | `ghcr.io/open-webui/open-webui` | 채팅, 지식 베이스(RAG), OAuth 토큰 교환 |
| `mcp-rag` | 이 저장소 `mcp-servers/openwebui-rag-newapi`에서 빌드 | **new-api 키 인증 MCP 서버** (포트 `MCP_PORT`, 기본 8000) |
| `mcp-rag-owui-key` (선택, `--profile owui-key`) | 이 저장소 `mcp-servers/openwebui-rag`에서 빌드 | Open WebUI API 키 인증 MCP 서버 (포트 8001) |
| `mock-upstream` (선택, `--profile mock`) | `python:3.12-alpine` | 모델 공급자 대신 쓰는 가짜 업스트림 |

검색 한 번은 이렇게 흘러갑니다.

```
MCP 클라이언트 ──Bearer sk-(new-api 키)──▶ mcp-rag
mcp-rag ──POST /oauth2/token-from-key──▶ new-api            → 액세스 토큰 (10분)
mcp-rag ──POST /api/v1/auths/oauth/oidc/token/exchange──▶ open-webui
open-webui ──GET userinfo / POST introspect──▶ new-api      (토큰 검증, PUBLIC_HOST 주소로)
open-webui ──▶ mcp-rag : Open WebUI JWT (최대 10분 캐시)
mcp-rag ──JWT──▶ open-webui /api/v1/knowledge, /api/v1/retrieval/query/collection
```

new-api 단독 배포(`deploy/openwebui-test`)와 다른 점은 아래뿐입니다.

- `mcp-rag` 서비스와, 선택 서비스인 `mcp-rag-owui-key`가 추가되었습니다.
- Open WebUI에 `ENABLE_API_KEYS`가 추가되었습니다. 선택 사항이며 기본값은 꺼짐입니다.
- `.env.example` 끝에 MCP 항목이 추가되었습니다.
- compose 프로젝트 이름은 `owui-rag-stack`, 네트워크 이름은 `owui-rag`입니다.

> new-api 단독 스택과 **동시에 띄우지 마세요.** 포트 3000과 8080이 겹칩니다. 한쪽을 `docker compose down`한 뒤 다른 쪽을 올리세요. 프로젝트 이름이 달라서 볼륨은 서로 섞이지 않습니다.

## 1. 준비물

1. **new-api 이미지:** new-api 저장소의 `OpenWebUI` 브랜치에서 빌드합니다. `/oauth2/token-from-key`가 들어간 커밋 `bf236c829` 이후여야 합니다. 빌드와 이동 방법은 new-api의 `deploy/openwebui-test/README.md` 1절을 그대로 따르고, 만든 태그를 `.env`의 `NEW_API_IMAGE`에 넣습니다.
2. **이 저장소 체크아웃:** `mcp-rag` 이미지는 이 저장소의 `mcp-servers/`에서 빌드합니다. compose가 `build: ../../mcp-servers/...`로 직접 빌드하므로 따로 할 일은 없습니다. 빌드에는 인터넷(PyPI)이 필요합니다. 오프라인 환경으로 옮긴다면 미리 빌드해 둡니다.

   ```bash
   docker compose build mcp-rag
   docker save openwebui-rag-mcp-newapi:local | gzip > openwebui-rag-mcp-newapi.tar.gz
   ```

3. 실행하는 머신에 bash, python3(표준 라이브러리만 사용), docker compose v2가 있어야 합니다(`bootstrap.sh`에 필요).

## 2. 호스트 이름 정하기 (가장 중요)

new-api README 2절과 같습니다. 브라우저와 Open WebUI 컨테이너가 new-api를 **같은 주소**(`http://PUBLIC_HOST:3000`)로 볼 수 있어야 합니다. `localhost`는 쓸 수 없습니다.

| 상황 | `PUBLIC_HOST` | 추가 설정 |
| --- | --- | --- |
| 같은 LAN, 공개 DNS 사용 가능 | `192-168-0-10.nip.io` | 없음 |
| 사내 DNS 이름이 있음 | 그 이름 | 없음 |
| 오프라인 | `newapi.local` 등 | 브라우저 머신의 `/etc/hosts`에 추가하고, `open-webui` 서비스의 `extra_hosts` 주석을 해제 |

MCP 서버는 new-api와 Open WebUI를 컨테이너 내부 이름(`new-api`, `open-webui`)으로 부르므로 이 설정과 무관합니다. 하지만 토큰을 검증하는 쪽은 Open WebUI이고, Open WebUI는 `PUBLIC_HOST` 주소로 new-api를 부릅니다. 따라서 이 설정이 틀리면 MCP도 동작하지 않습니다.

## 3. 실행 순서

```bash
cd deploy/openwebui-rag
cp .env.example .env
# .env 편집: PUBLIC_HOST, NEW_API_IMAGE, 비밀값, 계정 비밀번호(8~20자), 필요하면 MCP_BIND
docker compose up -d new-api            # postgres, redis, new-api
./bootstrap.sh                          # 루트·서비스 토큰·OIDC·token-from-key 옵션·테스트 사용자, OWUI_SERVICE_KEY 기록
docker compose up -d --build            # open-webui + mcp-rag
```

`bootstrap.sh`가 new-api에 해 주는 일 가운데 MCP와 관련된 것은 다음과 같습니다.
- `oauth2.token_from_key_enabled=true`로 "API 키로 토큰 발급" 기능을 켭니다.
- `oauth2.token_from_key_client_id`를 `OIDC_MCP_CLIENT_ID`(기본 `openwebui-mcp`)로 설정합니다.

이 스크립트는 여러 번 실행해도 안전합니다.

모델 공급자 키가 없으면 mock 업스트림을 씁니다.

```bash
# .env 에 MOCK_UPSTREAM=1, OWUI_EMBEDDING_MODEL=mock-gpt
docker compose --profile mock up -d new-api mock-upstream && ./bootstrap.sh && docker compose --profile mock up -d --build
```

mock의 임베딩은 모든 문장에 같은 벡터를 돌려줍니다. 그래서 **검색 흐름은 확인할 수 있지만, 결과의 관련도는 의미가 없습니다.**

Open WebUI 키 방식 MCP도 같이 띄우려면 `.env`에 `OWUI_ENABLE_API_KEYS=true`를 넣고 다음을 실행합니다.

```bash
docker compose --profile owui-key up -d --build
```

## 4. Open WebUI 쪽 수동 설정

먼저 new-api README 4절의 세 가지를 그대로 합니다.
1. 첫 로그인 사용자를 관리자로 지정합니다.
2. `X-Task` 헤더를 설정합니다.
3. 모델 공개 범위를 Public으로 바꿉니다.

RAG를 쓰려면 여기에 두 가지를 더합니다.

1. **임베딩 모델 채널:** Open WebUI는 `RAG_EMBEDDING_MODEL`(`.env`의 `OWUI_EMBEDDING_MODEL`, 기본 `text-embedding-3-small`)로 new-api의 `/v1/embeddings`를 부릅니다. new-api 콘솔에서 이 모델을 서비스하는 채널을 추가해야 합니다. 서비스 계정 토큰으로 청구됩니다.
   - 임베딩 모델을 나중에 바꾸면 이미 만든 지식 베이스를 다시 색인해야 합니다. 관리자 → Settings → Documents에서 재색인합니다.
2. **지식 베이스 만들기:** Open WebUI → 워크스페이스 → 지식에서 지식 베이스를 만들고 문서를 올립니다.
   - 다른 사용자도 검색해야 하면 접근 범위를 그 사용자나 그룹, 또는 Public으로 열어 둡니다.
   - **MCP 검색 결과는 Open WebUI 화면에서 그 사용자가 볼 수 있는 지식과 똑같습니다.**

## 5. 검수

### 5-1. 토큰 흐름 (curl)

`sk-…`는 **웹 OIDC로 Open WebUI에 한 번 이상 로그인한 사용자**의 new-api 키입니다. 서비스 계정 키가 아닙니다.

```bash
KEY=sk-...
AT=$(curl -s -X POST -H "Authorization: Bearer $KEY" http://PUBLIC_HOST:3000/oauth2/token-from-key | jq -r .access_token)
curl -s -H "Authorization: Bearer $AT" http://PUBLIC_HOST:3000/oauth2/userinfo
curl -s -X POST -H 'Content-Type: application/json' -d "{\"token\":\"$AT\"}" \
  http://PUBLIC_HOST:8080/api/v1/auths/oauth/oidc/token/exchange | jq '{token: (.token|.[0:20]), expires_at}'
```

### 5-2. MCP 서버 (스모크 테스트)

```bash
docker compose exec mcp-rag python /smoke_mcp.py "$KEY" "검색어"
```

정상이면 다음이 출력됩니다.
- `tools: list_knowledge_bases, search_knowledge`
- 지식 베이스 목록
- 검색 결과(`[1] 파일명 (score …)` 형식)

종료 코드는 0입니다.

### 5-3. 확인 표

| 확인 | 기대 결과 |
| --- | --- |
| 유효한 키로 스모크 테스트 | 목록과 검색이 성공함 |
| 없는 키 / 비활성 키 | `newapi API 키가 유효하지 않습니다` |
| 웹으로 로그인한 적 없는 사용자의 키 | `Open WebUI에 웹으로 한 번 로그인한 뒤 다시 시도하세요` |
| 다른 사용자에게만 공개한 지식 베이스 | 목록에 나오지 않고, id를 지정해 검색하면 접근 오류가 남 |
| new-api에서 그 키를 비활성화 | 늦어도 약 10분(토큰 수명) 안에 검색이 거부됨 |
| new-api 감사 로그 | 사용자 로그에 `oidc token-from-key issued token_id=… client_id=openwebui-mcp` 기록 |

로그 확인:
- `docker compose logs -f mcp-rag`
- `docker compose logs -f new-api | grep -i oidc`
- `docker compose logs -f open-webui | grep -i "token exchange"`

## 6. MCP 클라이언트 등록

각 사용자는 **자기 new-api API 키**를 씁니다. MCP 주소는 `http://<MCP 서버 호스트>:8000/mcp`입니다.

**MCP 서버를 다른 PC에서 쓰려면** `.env`에서 `MCP_BIND=0.0.0.0`으로 바꾸고 `docker compose up -d mcp-rag`로 다시 띄웁니다. 이 서버에는 HTTPS와 자체 접근 제어가 없으므로 **사내망 안에서만** 여세요.

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

- **키 하나가 곧 Open WebUI 검색 권한입니다.** new-api 키가 새면 그 사용자의 Open WebUI 지식도 읽힐 수 있습니다. 키를 비활성화하면 새 토큰 발급은 즉시 막힙니다. 다만 이미 발급된 토큰과 MCP 캐시 때문에, **최대 토큰 수명(기본 600초, new-api 설정상 최대 900초)까지는** 검색이 될 수 있습니다.
- **호출 IP는 MCP 서버입니다.**
  - **키의 IP 제한:** new-api 키에 IP 제한(AllowIps)을 걸었다면 MCP 서버의 IP를 허용해야 합니다. 이 스택에서는 compose 네트워크의 `mcp-rag` 컨테이너 주소입니다. 그렇지 않으면 `IP 제한에 MCP 서버 IP가 허용되어 있지 않습니다` 오류가 납니다.
  - **IP당 호출 제한:** new-api의 IP당 분당 60회 제한도 MCP 서버 하나에 모든 사용자가 몰립니다. 캐시 덕분에 사용자당 약 10분에 1회만 호출하므로 동시 사용자 수백 명까지는 문제없습니다.
- **Open WebUI 토큰 교환 호출 제한은 켜지 마세요.** `OAUTH_TOKEN_EXCHANGE_RATE_LIMIT`도 IP 기준이라 같은 문제가 생깁니다. compose에서는 설정하지 않았습니다.
- **허용 클라이언트:** `OAUTH_TOKEN_EXCHANGE_TRUSTED_CLIENT_IDS=openwebui-mcp`이므로 new-api가 MCP용으로 발급한 토큰만 교환됩니다. Open WebUI는 new-api의 introspection으로 이를 확인합니다. `OIDC_MCP_CLIENT_ID`를 바꾸면 bootstrap과 compose 양쪽에 같은 값이 들어갑니다.
- **사용자 조건:** MCP를 쓰려면 Open WebUI 웹에 **한 번 로그인**해 계정이 있어야 하고, new-api 계정에 **이메일**이 있어야 합니다(`email_required`).
- **여러 대로 늘릴 때:** MCP 서버를 여러 개 띄워도 됩니다. 캐시는 각 인스턴스의 메모리에 있으므로, 인스턴스마다 처음 한 번씩 교환이 일어날 뿐입니다.

## 8. 문제 해결 (MCP)

new-api와 Open WebUI 자체의 문제는 new-api README 7절을 보세요.

| 오류 메시지 | 원인과 조치 |
| --- | --- |
| `newapi 관리자가 'API 키로 토큰 발급' 기능을 켜야 합니다` | `./bootstrap.sh`를 다시 실행하거나, new-api 시스템 설정 → OpenWebUI 연동에서 켭니다 |
| `newapi의 OIDC 제공자가 꺼져 있거나 NEWAPI_URL/NEWAPI_TOKEN_PATH가 잘못되었습니다` | `oauth2.enabled`가 꺼져 있거나 new-api 이미지가 `bf236c829` 이전 버전입니다 |
| `newapi 계정에 이메일이 없습니다…` | 그 사용자의 new-api 프로필에 이메일을 등록합니다 |
| `Open WebUI 관리자가 ENABLE_OAUTH_TOKEN_EXCHANGE를 켜야 합니다` | compose 환경 변수가 반영되지 않은 것입니다. `docker compose up -d open-webui`로 다시 띄웁니다 |
| `Open WebUI에 웹으로 한 번 로그인한 뒤 다시 시도하세요` | 그 사용자가 Open WebUI에 웹으로 로그인한 적이 없거나, `OPENWEBUI_OAUTH_PROVIDER`가 `oidc`가 아닙니다 |
| `허용되지 않은 클라이언트이거나 이메일 도메인입니다…` | new-api의 `oauth2.token_from_key_client_id`와 Open WebUI의 `OAUTH_TOKEN_EXCHANGE_TRUSTED_CLIENT_IDS`가 다르거나, introspection 호출이 실패했습니다(`OIDC_CLIENT_SECRET` 불일치) |
| `newapi 토큰이 Open WebUI에서 거부되었습니다…` | Open WebUI가 userinfo나 introspection을 부르지 못했습니다. Open WebUI 컨테이너에서 `wget -qO- http://PUBLIC_HOST:3000/.well-known/openid-configuration`이 되는지 확인합니다(2절) |
| `Open WebUI 계정이 승인 대기 중이거나 MFA가 필요해…` | Admin Panel → Users에서 역할이 `pending`인지 확인합니다 |
| `인증 서버에 연결할 수 없습니다` | `mcp-rag`가 `new-api`나 `open-webui`에 닿지 못합니다. `docker compose ps`로 상태를 확인합니다 |
| 목록은 나오는데 검색 결과가 비어 있음 | 지식 베이스에 문서가 색인되지 않았거나 임베딩 채널이 없습니다. `docker compose logs open-webui`에서 embedding 오류를 확인합니다 |
| 클라이언트가 접속하지 못함 | `MCP_BIND=127.0.0.1`(기본값)이면 다른 PC에서는 접속할 수 없습니다(6절) |
