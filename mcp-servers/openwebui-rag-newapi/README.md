# Open WebUI RAG MCP 서버 (newapi 키 인증판)

Open WebUI의 지식 베이스(Knowledge)를 MCP 클라이언트(Claude Desktop, Claude Code, Cursor 등)에서 검색할 수 있게 해주는 MCP 서버입니다.

## 기존 판과의 차이

기존 판(`mcp-servers/openwebui-rag/`)은 사용자가 Open WebUI API 키를 직접 만들어 등록해야 합니다. 이 판은 **이미 쓰고 있는 newapi API 키 하나만** 등록하면 됩니다. Open WebUI 키나 이메일은 등록하지 않습니다.

동작 방식:

1. 사용자의 newapi 키로 `POST {NEWAPI_URL}{NEWAPI_TOKEN_PATH}`를 호출해 `access_token`을 받습니다.
2. 그 토큰을 Open WebUI의 토큰 교환 엔드포인트(`/api/v1/auths/oauth/{provider}/token/exchange`)에 보내 Open WebUI JWT를 받습니다.
3. JWT로 Open WebUI API를 호출합니다. JWT는 키별로 메모리에 캐시하며(키 원문의 SHA-256 해시로 보관), 만료 전에 갱신합니다.
   캐시 수명은 Open WebUI JWT 만료와 newapi 토큰 수명(`expires_in`, 기본 600초) 중 **짧은 쪽**입니다. 그래서 newapi에서 키나 사용자를 비활성화하면 늦어도 그 수명 안에 MCP 검색도 막힙니다.

제공 도구:

- `list_knowledge_bases`: 접근 가능한 지식 베이스의 id, 이름, 설명을 반환합니다.
- `search_knowledge(query, knowledge_ids?, k?)`: 지정한 지식 베이스(생략 시 접근 가능한 전체, 단 `MAX_COLLECTIONS` 이하일 때만)에서 관련 청크와 출처를 반환합니다. `k` 기본값은 5입니다.

## 관리자 체크리스트

1. Open WebUI에서 토큰 교환을 켭니다: `ENABLE_OAUTH_TOKEN_EXCHANGE=true`.
2. 권장: `OAUTH_TOKEN_EXCHANGE_TRUSTED_CLIENT_IDS=openwebui-mcp`. newapi가 이 client_id로 토큰을 발급하고 introspection(`{issuer}/oauth2/introspect`)을 지원하므로, 이 MCP 서버를 거친 토큰만 교환되게 제한할 수 있습니다. newapi 관리자 설정에서 client_id를 바꿨다면 그 값을 넣으세요.
3. 주의: `OAUTH_TOKEN_EXCHANGE_RATE_LIMIT`은 IP 기반입니다. 이 MCP 서버가 모든 사용자의 교환을 한 IP에서 보내므로, 사용자가 많으면 한도에 걸릴 수 있습니다. 한도를 넉넉히 잡으세요 (JWT 캐시로 호출 수는 줄어듭니다).
4. `OPENWEBUI_OAUTH_PROVIDER`는 Open WebUI에 등록된 provider 이름과 일치해야 합니다. 일반 OIDC 설정(`OPENID_PROVIDER_URL`)이면 `oidc`입니다. 계정은 provider 이름과 `sub`의 조합으로 찾으므로 틀리면 사용자를 찾지 못합니다.
5. newapi에서 **시스템 설정 → OpenWebUI 연동 → "API 키로 토큰 발급"**을 켭니다(기본 꺼짐). 꺼져 있으면 "newapi 관리자가 'API 키로 토큰 발급' 기능을 켜야 합니다" 오류가 납니다.
6. newapi의 IP 기준 호출 제한(IP당 분당 60회, 고정)도 MCP 서버 IP 하나에 모든 사용자가 몰립니다. 캐시 덕분에 사용자당 약 10분에 1회만 호출하므로 동시 사용자 수백 명까지는 문제없지만, 그 이상이면 newapi 쪽 조정이 필요합니다.
7. 사용자가 newapi 키에 **IP 제한(AllowIps)**을 걸어 두었다면, newapi가 보는 호출 IP는 사용자 PC가 아니라 **MCP 서버의 IP**입니다. 그 키에 MCP 서버 IP를 허용해야 합니다. 그렇지 않으면 "IP 제한에 MCP 서버 IP가 허용되어 있지 않습니다" 오류가 납니다.
8. newapi 계정에 이메일이 없으면 토큰이 발급되지 않습니다(`email_required`).
9. 사용자는 Open WebUI 웹 화면에 **한 번은 로그인**해 계정이 만들어져 있어야 합니다. 그렇지 않으면 "Open WebUI에 웹으로 한 번 로그인한 뒤 다시 시도하세요" 오류가 납니다.

## newapi 엔드포인트 의존성

newapi의 `POST {issuer}/oauth2/token-from-key`(키로 OIDC 호환 access token 발급)를 사용합니다. `NEWAPI_URL`에는 newapi OIDC issuer 주소(Open WebUI의 `OPENID_PROVIDER_URL`에서 `/.well-known/openid-configuration`을 뺀 부분)를 넣으세요. 요구서: [`docs/superpowers/specs/2026-10-31-newapi-key-to-oidc-token-requirements.md`](../../docs/superpowers/specs/2026-10-31-newapi-key-to-oidc-token-requirements.md).

## 환경 변수

| 변수 | 기본값 | 설명 |
|---|---|---|
| `OPENWEBUI_URL` | (필수) | Open WebUI 주소. 예: `http://localhost:8080` |
| `NEWAPI_URL` | (필수) | newapi OIDC issuer 주소. 예: `http://localhost:3000` |
| `NEWAPI_TOKEN_PATH` | `/oauth2/token-from-key` | newapi 토큰 발급 경로 |
| `OPENWEBUI_OAUTH_PROVIDER` | `oidc` | Open WebUI에 등록된 provider 이름 |
| `NEWAPI_API_KEY` | - | stdio 모드 전용. HTTP 모드에서는 사용되지 않음 |
| `MAX_COLLECTIONS` | `20` | `knowledge_ids` 생략 시 한 번에 검색할 수 있는 최대 지식 베이스 수 |
| `MCP_TRANSPORT` | `stdio` | `stdio` 또는 `http` (Docker 이미지는 `http`) |
| `MCP_HOST` | `0.0.0.0` | HTTP 모드 바인드 주소 |
| `MCP_PORT` | `8000` | HTTP 모드 포트 |

## 설치

```bash
cd mcp-servers/openwebui-rag-newapi
pip install .
```

## stdio 등록

### Claude Desktop (`claude_desktop_config.json`)

Claude Desktop은 셸의 `PATH`를 상속하지 않으므로, `command`에는 venv 안 실행 파일의 절대 경로를 사용하세요.

```json
{
  "mcpServers": {
    "openwebui-rag-newapi": {
      "command": "/path/to/.venv/bin/openwebui-rag-mcp-newapi",
      "env": {
        "OPENWEBUI_URL": "http://localhost:8080",
        "NEWAPI_URL": "http://localhost:3000",
        "OPENWEBUI_OAUTH_PROVIDER": "oidc",
        "NEWAPI_API_KEY": "sk-..."
      }
    }
  }
}
```

### Claude Code

```bash
claude mcp add openwebui-rag-newapi \
  -e OPENWEBUI_URL=http://localhost:8080 \
  -e NEWAPI_URL=http://localhost:3000 \
  -e OPENWEBUI_OAUTH_PROVIDER=oidc \
  -e NEWAPI_API_KEY=sk-... \
  -- openwebui-rag-mcp-newapi
```

### Cursor (`.cursor/mcp.json`)

```json
{
  "mcpServers": {
    "openwebui-rag-newapi": {
      "command": "openwebui-rag-mcp-newapi",
      "env": {
        "OPENWEBUI_URL": "http://localhost:8080",
        "NEWAPI_URL": "http://localhost:3000",
        "OPENWEBUI_OAUTH_PROVIDER": "oidc",
        "NEWAPI_API_KEY": "sk-..."
      }
    }
  }
}
```

## 컨테이너 실행 (HTTP 모드)

```bash
docker build -t openwebui-rag-mcp-newapi mcp-servers/openwebui-rag-newapi
docker run --rm -p 8000:8000 \
  -e OPENWEBUI_URL=http://host.docker.internal:8080 \
  -e NEWAPI_URL=http://host.docker.internal:3000 \
  -e OPENWEBUI_OAUTH_PROVIDER=oidc \
  --add-host=host.docker.internal:host-gateway \
  openwebui-rag-mcp-newapi
```

엔드포인트는 `http://localhost:8000/mcp` (Streamable HTTP)입니다.

### docker-compose 예시

```yaml
services:
  openwebui-rag-mcp-newapi:
    build: ./mcp-servers/openwebui-rag-newapi
    environment:
      - OPENWEBUI_URL=http://open-webui:8080
      - NEWAPI_URL=http://newapi:3000
      - OPENWEBUI_OAUTH_PROVIDER=oidc
    ports:
      # 기본은 호스트 로컬에서만 접근 가능합니다.
      # 내부망에 열려면 "<내부망 IP>:8000:8000"으로 바꾸세요 (방화벽/리버스 프록시로 접근 제한).
      - "127.0.0.1:8000:8000"
```

## HTTP 등록 (내부망 전용)

HTTP 모드에서는 각 클라이언트가 자신의 newapi 키를 `Authorization: Bearer sk-...` 헤더로 보내야 합니다. 환경 변수로 대체(fallback)하지 않으며, 헤더가 없으면 도구가 "No API key provided" 오류를 반환합니다. 이 서버 자체에는 별도 인증이 없으므로 인터넷에 노출하지 말고 내부망에서만 사용하세요.

```bash
claude mcp add --transport http openwebui-rag-newapi http://localhost:8000/mcp \
  --header "Authorization: Bearer sk-..."
```

Cursor (`.cursor/mcp.json`):

```json
{
  "mcpServers": {
    "openwebui-rag-newapi": {
      "url": "http://localhost:8000/mcp",
      "headers": { "Authorization": "Bearer sk-..." }
    }
  }
}
```
