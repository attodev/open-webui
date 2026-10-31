# Open WebUI RAG MCP 서버 설계

## 목적과 이유

Open WebUI에서 만들어 둔 지식(Knowledge) 컬렉션을 Open WebUI 밖의 MCP 클라이언트(Claude Desktop, Claude Code, Cursor 등)에서도 검색할 수 있게 하라는 요구 사항이 있다. 특정 사용 시나리오가 정해진 것은 아니므로 일반적인 MCP 클라이언트라면 어디서나 등록해 쓸 수 있어야 한다.

## 목표

- MCP 서버를 하나 만든다. 이 서버는 Open WebUI의 지식 컬렉션 검색만 외부에 제공한다.
- 검색 결과는 Open WebUI 안에서 RAG를 쓸 때와 같아야 한다. 임베딩 모델, 하이브리드 검색, 리랭킹, top_k, 관련도 임계값 같은 설정을 Open WebUI에 설정된 그대로 따른다.
- 표준 MCP 클라이언트에 등록하는 방법을 문서로 제공한다.

## 비목표

- 문서 업로드, 컬렉션 생성·수정·삭제, 임베딩/재색인 같은 관리 기능은 만들지 않는다. 관리는 계속 Open WebUI에서 한다.
- 답변 생성(LLM 호출)은 하지 않는다. 검색 결과를 받아 답을 만드는 일은 클라이언트의 LLM이 맡는다.
- Open WebUI 없이 단독으로 동작하는 RAG는 만들지 않는다.

## 피해야 할 결과

- Open WebUI와 다른 검색 결과가 나오는 경우. 벡터 DB에 직접 붙어서 검색 로직을 다시 구현하면 이렇게 되기 쉽다.
- 사용자가 Open WebUI에서는 볼 수 없는 컬렉션을 외부에서는 검색할 수 있는 경우.
- Open WebUI 본체 코드를 고쳐야만 동작해서 업스트림 업데이트를 따라가기 어려워지는 경우.

## 확정된 방식

- **Open WebUI HTTP API를 감싸는 얇은 래퍼**로 만든다. 사용자가 확인함: "기본은 OpenWebUI 그대로 가고, RAG 검색만 외부에서도 가능하게".
- 저장소를 확인한 결과 필요한 API가 이미 있다.
  - `GET /api/v1/knowledge/`는 호출한 사용자가 접근할 수 있는 지식 컬렉션 목록을 준다 (`backend/open_webui/routers/knowledge.py:173`).
  - `POST /api/v1/retrieval/query/collection`은 `collection_names`, `query`와 선택 항목인 `k`, `k_reranker`, `r`, `hybrid`를 받는다. 하이브리드 검색, 리랭킹, 설정 기본값을 Open WebUI와 똑같이 적용하고, `_validate_collection_access`로 접근 권한도 확인한다 (`routers/retrieval.py:2872`).
  - 인증에는 Open WebUI API 키(`sk-…`)를 Bearer 토큰으로 쓸 수 있다 (`utils/auth.py:415`).

## 내가 정한 사항 (검토 필요)

- **권한 방식:** 사용자별 Open WebUI API 키를 쓴다. 각 사용자는 자기 클라이언트 설정에 자기 키를 넣는다. 그러면 Open WebUI가 권한을 판단하므로 "피해야 할 결과"의 두 번째 항목이 자동으로 지켜진다. 공용 서비스 계정 하나만 쓰려면 그 계정의 키를 넣으면 되므로 별도 구현은 필요 없다.
- **언어와 SDK:** Python과 공식 `mcp` SDK(FastMCP), HTTP 클라이언트는 `httpx`를 쓴다. 백엔드와 같은 언어여서 유지보수가 쉽다.
- **위치:** 이 저장소의 `mcp-servers/openwebui-rag/`에 독립 패키지로 둔다. Open WebUI 백엔드 코드는 import하지 않는다.
- **전송 방식:** 로컬 등록용 stdio를 기본으로 하고, 원격 공유용 Streamable HTTP를 옵션으로 둔다. HTTP 모드에서는 클라이언트가 보낸 `Authorization` 헤더를 Open WebUI로 그대로 전달한다.
- **설정:** 환경 변수 `OPENWEBUI_URL`과 `OPENWEBUI_API_KEY`(stdio 모드용)를 쓴다.
- **도구:**
  1. `list_knowledge_bases()`는 id, 이름, 설명을 돌려준다.
  2. `search_knowledge(query, knowledge_ids, k?)`는 문서 조각마다 본문, 출처 파일명, 관련도 점수를 돌려준다. `knowledge_ids`를 생략하면 접근 가능한 모든 컬렉션을 검색한다.
  3. 컬렉션 이름은 지식 id를 그대로 쓴다. 구현할 때 query/collection이 지식 id를 컬렉션 이름으로 받는지 확인해야 한다.
- **`knowledge_ids` 생략 시:** 접근 가능한 모든 컬렉션을 검색하되, 최대 개수(기본 20, 환경 변수로 조정)를 넘으면 id를 지정하라는 오류를 돌려준다.
- **관리자 사전 설정:** README에 체크리스트를 둔다. API 키 기능 켜기(`auth.enable_api_keys`), 엔드포인트 제한을 쓰는 경우 `/api/v1/knowledge`와 `/api/v1/retrieval/query/collection` 허용, 사용자 키 생성 권한.
- **지원 버전:** 이 저장소의 현재 버전(0.12.x)만 지원한다. knowledge 목록 응답이 리스트로 오든 `{items: [...]}`로 오든 둘 다 처리한다.
- **오류:** 401/403/404는 사람이 읽을 수 있는 MCP 도구 오류로 바꿔서 돌려준다.
- **테스트:** Open WebUI API를 모킹한 단위 테스트를 만들고, 실제 인스턴스로 수동 확인한다.
- **문서:** README에 Claude Desktop, Claude Code, Cursor 등록 예시를 넣는다.

## 사용자 결정 사항

- **결과 일치 범위:** Open WebUI의 검색 설정(전역 RAG 설정)과 일치하면 충분하다. 채팅에서 하는 질의 재작성, 전체 문서 컨텍스트 모드, 모델별 지식 설정은 범위 밖이다.
- **HTTP 모드:** 신뢰하는 내부 클라이언트용이다. Bearer 헤더 전달만 지원하고 OAuth/SSO는 만들지 않는다.
- **컨테이너:** Dockerfile을 제공해 컨테이너로 실행할 수 있게 한다. 컨테이너에서는 Streamable HTTP 모드로 뜨고, 포트와 `OPENWEBUI_URL`을 환경 변수로 받는다. docker-compose 예시도 README에 넣는다.

## 구현하는 사람이 정할 사항

- 결과를 출력하는 상세 형식, 응답 텍스트 길이 제한
- 패키징 방식(`uvx` 실행 지원 여부 등)
