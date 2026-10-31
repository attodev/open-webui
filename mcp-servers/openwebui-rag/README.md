# Open WebUI RAG MCP 서버

Open WebUI의 지식 베이스(Knowledge)를 MCP 클라이언트(Claude Desktop, Claude Code, Cursor 등)에서 검색할 수 있게 해주는 MCP 서버입니다.

## 기능과 한계

제공 도구:

- `list_knowledge_bases`: 내가 접근 가능한 지식 베이스의 id, 이름, 설명을 반환합니다.
- `search_knowledge(query, knowledge_ids?, k?)`: 지정한 지식 베이스(생략 시 접근 가능한 전체, 단 `MAX_COLLECTIONS` 이하일 때만)에서 가장 관련 있는 청크와 출처를 반환합니다. `k` 기본값은 5입니다.

한계:

- Open WebUI에 설정된 검색 설정(임베딩, 하이브리드 검색, 리랭커 등)만 그대로 사용합니다.
- 쿼리 재작성(query rewriting)과 전체 컨텍스트(full-context) 모드는 지원하지 않습니다.

## 관리자 체크리스트

1. Open WebUI에서 API 키를 켭니다: 환경 변수 `ENABLE_API_KEYS=true` (관리자 설정 > 일반 > API 키 활성화).
2. API 키 엔드포인트 제한을 사용 중이라면(`ENABLE_API_KEYS_ENDPOINT_RESTRICTIONS=true`), `API_KEYS_ALLOWED_ENDPOINTS`에 다음 두 경로를 허용합니다.
   - `/api/v1/knowledge`
   - `/api/v1/retrieval/query/collection`
3. 사용자가 API 키를 만들 수 있어야 합니다. 사용자는 설정 > 계정에서 `sk-...` 키를 발급합니다.

## 환경 변수

| 변수 | 기본값 | 설명 |
|---|---|---|
| `OPENWEBUI_URL` | (필수) | Open WebUI 주소. 예: `http://localhost:8080` |
| `OPENWEBUI_API_KEY` | - | stdio 모드 전용. HTTP 모드에서는 사용되지 않음 |
| `MAX_COLLECTIONS` | `20` | `knowledge_ids` 생략 시 한 번에 검색할 수 있는 최대 지식 베이스 수 |
| `MCP_TRANSPORT` | `stdio` | `stdio` 또는 `http` (Docker 이미지는 `http`) |
| `MCP_HOST` | `0.0.0.0` | HTTP 모드 바인드 주소 |
| `MCP_PORT` | `8000` | HTTP 모드 포트 |

## 설치

```bash
cd mcp-servers/openwebui-rag
pip install .
```

## stdio 등록

### Claude Desktop (`claude_desktop_config.json`)

Claude Desktop은 셸의 `PATH`를 상속하지 않으므로, `command`에는 venv 안의 `openwebui-rag-mcp` 실행 파일의 절대 경로를 사용하세요 (예: `/path/to/.venv/bin/openwebui-rag-mcp`).

```json
{
  "mcpServers": {
    "openwebui-rag": {
      "command": "/path/to/.venv/bin/openwebui-rag-mcp",
      "env": {
        "OPENWEBUI_URL": "http://localhost:8080",
        "OPENWEBUI_API_KEY": "sk-..."
      }
    }
  }
}
```

### Claude Code

```bash
claude mcp add openwebui-rag \
  -e OPENWEBUI_URL=http://localhost:8080 \
  -e OPENWEBUI_API_KEY=sk-... \
  -- openwebui-rag-mcp
```

### Cursor (`.cursor/mcp.json`)

```json
{
  "mcpServers": {
    "openwebui-rag": {
      "command": "openwebui-rag-mcp",
      "env": {
        "OPENWEBUI_URL": "http://localhost:8080",
        "OPENWEBUI_API_KEY": "sk-..."
      }
    }
  }
}
```

## 컨테이너 실행 (HTTP 모드)

```bash
docker build -t openwebui-rag-mcp mcp-servers/openwebui-rag
docker run --rm -p 8000:8000 \
  -e OPENWEBUI_URL=http://host.docker.internal:8080 \
  --add-host=host.docker.internal:host-gateway \
  openwebui-rag-mcp
```

엔드포인트는 `http://localhost:8000/mcp` (Streamable HTTP)입니다.

### docker-compose 예시

`open-webui` 서비스와 같은 compose 파일에 추가합니다.

```yaml
services:
  open-webui:
    image: ghcr.io/open-webui/open-webui:main
    environment:
      - ENABLE_API_KEYS=true
    ports:
      - "8080:8080"

  openwebui-rag-mcp:
    build: ./mcp-servers/openwebui-rag
    environment:
      - OPENWEBUI_URL=http://open-webui:8080
    ports:
      # 기본은 호스트 로컬에서만 접근 가능합니다.
      # 내부망에 열려면 "<내부망 IP>:8000:8000"으로 바꾸거나 "8000:8000"으로 모든 인터페이스에 공개하세요
      # (방화벽/리버스 프록시로 접근을 제한할 것).
      - "127.0.0.1:8000:8000"
    depends_on:
      - open-webui
```

## HTTP 등록 (내부망 전용)

HTTP 모드에서는 각 클라이언트가 자신의 Open WebUI API 키를 `Authorization: Bearer sk-...` 헤더로 보내야 합니다. 서버의 `OPENWEBUI_API_KEY`로 대체(fallback)하지 않으며, 헤더가 없으면 도구가 "No API key provided" 오류를 반환합니다. 이 서버 자체에는 별도 인증이 없으므로 인터넷에 노출하지 말고 내부망에서만 사용하세요.

Claude Code 예시:

```bash
claude mcp add --transport http openwebui-rag http://localhost:8000/mcp \
  --header "Authorization: Bearer sk-..."
```

Cursor 예시 (`.cursor/mcp.json`):

```json
{
  "mcpServers": {
    "openwebui-rag": {
      "url": "http://localhost:8000/mcp",
      "headers": { "Authorization": "Bearer sk-..." }
    }
  }
}
```
