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

### 파일

| 파일 | 용도 |
| --- | --- |
| `setup.sh` | **처음 연동(또는 설정 변경 후 재연동)을 한 번에 처리합니다.** 아래 스크립트들을 순서대로 실행합니다. |
| `bootstrap.sh` | new-api 설정입니다. new-api 저장소의 사본이며 수정하지 않았습니다. |
| `bootstrap-rag.sh` | RAG·MCP 연동: 임베딩 채널, 연결 확인, 테스트 사용자 첫 로그인, 샘플 지식, MCP 스모크 테스트 |
| `docker-compose.yml` | 연동용입니다. MCP 이미지를 빌드하고 `setup.sh`가 사용합니다. |
| `docker-compose.run.yml` | **운영·재시작용입니다.** 빌드 없이 이미지만으로 띄우며, `.env`에 연동 결과가 들어 있어야 합니다. |
| `smoke_mcp.py` | MCP 동작 확인 스크립트로, MCP 컨테이너 안에서 실행합니다. |
| `mock_upstream.py` | 가짜 모델 공급자입니다. new-api 저장소의 사본입니다. |

new-api 단독 배포(`deploy/openwebui-test`)와 다른 점:
- **서비스 추가:** `mcp-rag`와 선택 서비스 `mcp-rag-owui-key`가 추가되었습니다.
- **Open WebUI 수동 설정을 환경 변수로 옮겼습니다**(4절).
- **새 스크립트:** `setup.sh`, `bootstrap-rag.sh`, `docker-compose.run.yml`이 추가되었습니다.
- **이름:** compose 프로젝트 이름은 `owui-rag-stack`, 네트워크 이름은 `owui-rag`입니다.

> new-api 단독 스택과 **동시에 띄우지 마세요.** 포트 3000과 8080이 겹칩니다. 한쪽을 `docker compose down`한 뒤 다른 쪽을 올리세요. 프로젝트 이름이 달라서 볼륨은 서로 섞이지 않습니다.

## 1. 준비물

1. **new-api 이미지:** new-api 저장소의 `OpenWebUI` 브랜치에서 빌드합니다. `/oauth2/token-from-key`가 들어간 커밋 `bf236c829` 이후여야 합니다. 빌드와 이동 방법은 new-api의 `deploy/openwebui-test/README.md` 1절을 그대로 따르고, 만든 태그를 `.env`의 `NEW_API_IMAGE`에 넣습니다.
2. **이 저장소 체크아웃:** `mcp-rag` 이미지는 이 저장소의 `mcp-servers/`에서 빌드합니다. compose가 `build: ../../mcp-servers/...`로 직접 빌드하므로 따로 할 일은 없습니다. 빌드에는 인터넷(PyPI)이 필요합니다. 오프라인 환경으로 옮긴다면 미리 빌드해 둡니다.

   ```bash
   docker compose build mcp-rag
   docker save openwebui-rag-mcp-newapi:local | gzip > openwebui-rag-mcp-newapi.tar.gz
   ```

3. 실행하는 머신에 bash, python3(표준 라이브러리만 사용), docker compose v2가 있어야 합니다(`setup.sh`와 bootstrap 스크립트에 필요).

## 2. 호스트 이름 정하기 (가장 중요)

new-api README 2절과 같습니다. 브라우저와 Open WebUI 컨테이너가 new-api를 **같은 주소**(`http://PUBLIC_HOST:3000`)로 볼 수 있어야 합니다. `localhost`는 쓸 수 없습니다.

| 상황 | `PUBLIC_HOST` | 추가 설정 |
| --- | --- | --- |
| 같은 LAN, 공개 DNS 사용 가능 | `192-168-0-10.nip.io` | 없음 |
| 사내 DNS 이름이 있음 | 그 이름 | 없음 |
| 오프라인 | `newapi.local` 등 | 브라우저 머신의 `/etc/hosts`에 추가하고, `open-webui` 서비스의 `extra_hosts` 주석을 해제 |

MCP 서버는 new-api와 Open WebUI를 컨테이너 내부 이름(`new-api`, `open-webui`)으로 부르므로 이 설정과 무관합니다. 하지만 토큰을 검증하는 쪽은 Open WebUI이고, Open WebUI는 `PUBLIC_HOST` 주소로 new-api를 부릅니다. 따라서 이 설정이 틀리면 MCP도 동작하지 않습니다.

## 3. 처음 연동: `setup.sh`

```bash
cd deploy/openwebui-rag
cp .env.example .env
# .env 편집: PUBLIC_HOST, NEW_API_IMAGE, 비밀값, 계정 비밀번호(8~20자),
#            임베딩 채널(EMBEDDING_CHANNEL_KEY) 또는 MOCK_UPSTREAM=1, 필요하면 MCP_BIND
./setup.sh
```

`setup.sh`는 다음을 순서대로 하고, 하나라도 실패하면 이유를 출력하고 멈춥니다. 여러 번 실행해도 안전합니다.

| 단계 | 내용 |
| --- | --- |
| 1 | `new-api`, DB, Redis를 띄웁니다. `MOCK_UPSTREAM=1`이면 `mock-upstream`도 띄웁니다. |
| 2 | `bootstrap.sh`: 루트, 서비스 토큰, OIDC 클라이언트, "API 키로 토큰 발급"(`openwebui-mcp`), 테스트 사용자와 이메일을 설정합니다. **`OWUI_SERVICE_KEY`를 `.env`에 기록합니다.** |
| 3 | `bootstrap-rag.sh pre`: **임베딩 채널**을 준비합니다. mock이면 `mock-embedding` 채널을, `EMBEDDING_CHANNEL_KEY`가 있으면 실제 공급자 채널을 만듭니다. 둘 다 없으면 경고만 합니다. |
| 4 | Open WebUI와 MCP 서버를 빌드하고 띄운 뒤, 상태가 healthy가 될 때까지 기다립니다. 단계 2에서 바뀐 키를 반영하도록 컨테이너를 다시 만듭니다. |
| 5 | `bootstrap-rag.sh post`: 아래 내용을 처리합니다. |

단계 5에서 하는 일:
- 토큰 교환이 켜져 있는지, Open WebUI 컨테이너가 new-api discovery에 닿는지 확인합니다.
- `NEW_API_TEST_USERS`를 **OIDC로 Open WebUI에 한 번 로그인시켜** 계정을 만듭니다.
- 첫 테스트 사용자에게 new-api 키 `mcp`를 만들고, 그 사용자 이름으로 **'MCP 샘플' 지식 베이스**를 만들어 문서를 색인합니다.
- **MCP 스모크 테스트**로 목록과 검색을 확인합니다.

끝나면 다음과 같은 줄이 출력됩니다.

```
[bootstrap-rag] MCP 스모크 테스트 통과
```

**선택 항목**
- **mock 업스트림:** 모델 공급자 키가 없으면 `.env`에 `MOCK_UPSTREAM=1`을 둡니다. mock의 임베딩은 모든 문장에 같은 벡터를 돌려주므로 **검색 흐름은 확인할 수 있지만 관련도는 의미가 없습니다.**
- **Open WebUI 키 방식 MCP:** `.env`에 `OWUI_ENABLE_API_KEYS=true`를 두면 `setup.sh`가 `mcp-rag-owui-key`도 함께 띄웁니다.
- **실행 위치:** `setup.sh`와 `bootstrap-rag.sh`는 호스트에서 `http://PUBLIC_HOST:포트`로 new-api와 Open WebUI를 부릅니다. 그래서 실행하는 머신에서도 `PUBLIC_HOST`가 풀려야 합니다(2절).

## 4. 무엇이 자동이고 무엇이 남는가

### Open WebUI 설정은 모두 환경 변수로

Open WebUI는 기본적으로 처음 뜰 때 받은 환경 변수를 DB에 저장하고, 그 뒤로는 DB 값을 씁니다. 그래서 `.env`를 고치고 재시작해도 서비스 키나 임베딩 설정이 바뀌지 않습니다. 이 스택은 **`ENABLE_PERSISTENT_CONFIG=false`**로 두어 **매번 환경 변수를 읽게** 했습니다.

| new-api README 4절의 수동 설정 | 이 스택에서 대신하는 환경 변수 |
| --- | --- |
| 첫 로그인 사용자를 관리자로 | `ENABLE_OAUTH_ROLE_MANAGEMENT=true`(기존): new-api 관리자·루트는 로그인할 때마다 Open WebUI `admin`, 나머지는 `user` |
| `X-Task` 헤더 | `OPENAI_API_CONFIGS={"0":{"enable":true,"headers":{"X-Task":"{{TASK}}"}}}` |
| 모델 공개 범위 Public | `BYPASS_MODEL_ACCESS_CONTROL=true`(`OWUI_BYPASS_MODEL_ACCESS_CONTROL`): 모델 제한은 new-api 쪽에서만 합니다 |
| (RAG) 일반 사용자의 지식 베이스 만들기 권한 | `USER_PERMISSIONS_WORKSPACE_KNOWLEDGE_ACCESS=true`(`OWUI_USER_KNOWLEDGE_ACCESS`): Open WebUI 기본값은 관리자만입니다 |

**대가:** 관리자 화면에서 바꾼 설정은 **재시작하면 환경 변수 값으로 돌아갑니다.** 계속 쓸 설정은 compose나 `.env`에서 바꾸세요. 지식 베이스, 문서, 채팅, 사용자는 DB에 저장되므로 해당하지 않습니다.

### 남는 수동 작업

- **실제 모델 채널:** 채팅 모델 채널은 new-api 콘솔에서 추가합니다. 임베딩 채널은 `EMBEDDING_CHANNEL_KEY`로 자동 생성할 수 있습니다.
- **실제 사용자의 첫 로그인:** 실제 사용자는 Open WebUI를 브라우저로 한 번 열어 new-api로 로그인해야 계정이 생깁니다. 테스트 사용자만 자동입니다.
- **실제 사용자의 키와 MCP 등록:** 각자 new-api 콘솔에서 키를 만들어 MCP 클라이언트에 넣습니다(6절).
- **지식 베이스:** 실제 지식 베이스는 각자 Open WebUI → 워크스페이스 → 지식에서 만듭니다.
  - 다른 사용자도 검색해야 하면 접근 범위를 그 사용자나 그룹, 또는 Public으로 엽니다.
  - **MCP 검색 결과는 그 사용자가 Open WebUI 화면에서 볼 수 있는 지식과 똑같습니다.**
- **임베딩 모델 교체:** 임베딩 모델을 바꾸면 기존 지식 베이스를 다시 색인해야 합니다. 관리자 → Settings → Documents에서 합니다.

## 4-1. 연동이 끝난 뒤 재시작·운영: `docker-compose.run.yml`

```bash
docker compose -f docker-compose.run.yml up -d            # 기동·재시작 (MOCK_UPSTREAM=1 이면 --profile mock 추가)
docker compose -f docker-compose.run.yml ps
docker compose -f docker-compose.run.yml down             # 중지 (데이터 볼륨 유지)
```

- **빌드 없음:** 이미지만으로 띄웁니다. MCP 이미지는 `.env`의 `MCP_IMAGE`(기본 `openwebui-rag-mcp-newapi:local`)를 씁니다. 다른 머신에서 운영하려면 `.env`, 이 폴더의 파일들, 이미지(`docker save`/`load` 또는 레지스트리)만 있으면 됩니다.
- **연동 결과 필수:** `.env`에 `OWUI_SERVICE_KEY`가 없으면 시작하지 않습니다.
- **같은 데이터:** 프로젝트 이름과 볼륨이 `docker-compose.yml`과 같아서 같은 데이터를 씁니다. 두 파일 중 어느 쪽으로 내려도 데이터는 남습니다. `down -v`만 데이터를 지웁니다.
- **설정 변경:** `.env`를 고친 뒤 `docker compose -f docker-compose.run.yml up -d`를 하면 바로 반영됩니다(4절).
  - 서비스 토큰을 다시 발급했거나 new-api 쪽 설정(issuer 주소, 비밀값 등)을 바꿨다면 `./setup.sh`를 다시 실행하세요.
- **확인만 다시 하기:** 재시작 뒤 연동 상태를 확인하려면 다음을 실행합니다.

  ```bash
  COMPOSE_FILE_ARG=docker-compose.run.yml ./bootstrap-rag.sh post
  ```

- **서비스 정의를 바꿀 때:** `docker-compose.yml`과 `docker-compose.run.yml`의 서비스 정의는 같아야 합니다. 차이는 `build:`와 서비스 키 필수 여부뿐입니다. 한쪽을 바꾸면 다른 쪽도 같이 바꾸세요.

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
docker compose -f docker-compose.run.yml exec mcp-rag python /smoke_mcp.py "$KEY" "검색어"
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

**MCP 서버를 다른 PC에서 쓰려면** `.env`에서 `MCP_BIND=0.0.0.0`으로 바꾸고 `docker compose -f docker-compose.run.yml up -d mcp-rag`로 다시 띄웁니다. 이 서버에는 HTTPS와 자체 접근 제어가 없으므로 **사내망 안에서만** 여세요.

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
| `Open WebUI 관리자가 ENABLE_OAUTH_TOKEN_EXCHANGE를 켜야 합니다` | compose 환경 변수가 반영되지 않은 것입니다. `docker compose -f docker-compose.run.yml up -d open-webui`로 다시 띄웁니다 |
| `Open WebUI에 웹으로 한 번 로그인한 뒤 다시 시도하세요` | 그 사용자가 Open WebUI에 웹으로 로그인한 적이 없거나, `OPENWEBUI_OAUTH_PROVIDER`가 `oidc`가 아닙니다 |
| `허용되지 않은 클라이언트이거나 이메일 도메인입니다…` | new-api의 `oauth2.token_from_key_client_id`와 Open WebUI의 `OAUTH_TOKEN_EXCHANGE_TRUSTED_CLIENT_IDS`가 다르거나, introspection 호출이 실패했습니다(`OIDC_CLIENT_SECRET` 불일치) |
| `newapi 토큰이 Open WebUI에서 거부되었습니다…` | Open WebUI가 userinfo나 introspection을 부르지 못했습니다. Open WebUI 컨테이너에서 `wget -qO- http://PUBLIC_HOST:3000/.well-known/openid-configuration`이 되는지 확인합니다(2절) |
| `Open WebUI 계정이 승인 대기 중이거나 MFA가 필요해…` | Admin Panel → Users에서 역할이 `pending`인지 확인합니다 |
| `인증 서버에 연결할 수 없습니다` | `mcp-rag`가 `new-api`나 `open-webui`에 닿지 못합니다. `docker compose ps`로 상태를 확인합니다 |
| `setup.sh`: `지식 베이스 생성: HTTP 401` | `OWUI_USER_KNOWLEDGE_ACCESS=false`인데 첫 테스트 사용자가 관리자가 아닙니다. true로 두거나 `SEED_SAMPLE_KNOWLEDGE=0`으로 끕니다 |
| `setup.sh`: `파일 업로드/색인` 실패 | 임베딩 채널이 없거나 모델 이름이 다릅니다(`OWUI_EMBEDDING_MODEL`, 3절 단계 3) |
| `setup.sh`: `OIDC 로그인` 실패 | 호스트에서 `PUBLIC_HOST`가 풀리지 않거나, 테스트 사용자에게 이메일이 없습니다 |
| 목록은 나오는데 검색 결과가 비어 있음 | 지식 베이스에 문서가 색인되지 않았거나 임베딩 채널이 없습니다. `docker compose logs open-webui`에서 embedding 오류를 확인합니다 |
| 클라이언트가 접속하지 못함 | `MCP_BIND=127.0.0.1`(기본값)이면 다른 PC에서는 접속할 수 없습니다(6절) |
