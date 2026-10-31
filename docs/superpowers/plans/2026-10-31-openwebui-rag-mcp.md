# Open WebUI RAG MCP Server Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An MCP server that exposes Open WebUI knowledge-base search (list + search) to any MCP client, runnable via stdio or as a Docker container over Streamable HTTP.

**Architecture:** Standalone Python package in `mcp-servers/openwebui-rag/` wrapping two Open WebUI HTTP APIs with `httpx`. No imports from the Open WebUI backend. Auth = caller's Open WebUI API key as Bearer token (env var in stdio mode, passed-through `Authorization` header in HTTP mode).

**Tech Stack:** Python ≥3.11, `mcp` (FastMCP), `httpx`, `pytest`, `pytest-asyncio`, `respx`, Docker.

**Spec:** `docs/superpowers/specs/2026-10-31-openwebui-rag-mcp-design.md`

## Global Constraints

- Do not modify anything under `backend/` or `src/`.
- Env vars: `OPENWEBUI_URL` (required), `OPENWEBUI_API_KEY` (stdio mode), `MAX_COLLECTIONS` (default `20`), `MCP_TRANSPORT` (`stdio` default | `http`), `MCP_HOST` (default `0.0.0.0`), `MCP_PORT` (default `8000`).
- Open WebUI APIs: `GET {OPENWEBUI_URL}/api/v1/knowledge/?page=N` → `{items: [...], total}` (paginated; also accept a bare list); `POST {OPENWEBUI_URL}/api/v1/retrieval/query/collection` body `{collection_names, query, k?}` → `{documents: [[str]], metadatas: [[dict]], distances: [[float]]}`.
- The collection name equals the knowledge id.
- Do not send `hybrid`, `r`, or `k_reranker`, so Open WebUI's global settings apply.
- No OAuth. HTTP mode only forwards the Bearer header.

## Review Focus

- A missing or invalid API key → tool error "Open WebUI rejected the API key (401)…", not a stack trace. Covered in Task 1 test `test_401_maps_to_auth_error`.
- A knowledge id the user can't access → 403/404 mapped to a readable error. Covered in Task 1 `test_403_maps_to_access_error`.
- `knowledge_ids` omitted and the user has more than `MAX_COLLECTIONS` collections → an error asking for explicit ids. Covered in Task 2 `test_search_all_exceeds_cap`.
- A knowledge list spanning several pages → all pages are fetched. Covered in Task 1 `test_list_paginates`.
- Empty search results → "No results." instead of an IndexError. Covered in Task 2 `test_search_empty`.

---

### Task 1: Open WebUI API client

**Files:**
- Create: `mcp-servers/openwebui-rag/pyproject.toml` (package `openwebui_rag_mcp`, script `openwebui-rag-mcp = openwebui_rag_mcp.server:main`, dev deps pytest, pytest-asyncio, respx)
- Create: `mcp-servers/openwebui-rag/src/openwebui_rag_mcp/__init__.py`
- Create: `mcp-servers/openwebui-rag/src/openwebui_rag_mcp/client.py`
- Test: `mcp-servers/openwebui-rag/tests/test_client.py`

**Interfaces:**
- Produces:
  - `class OpenWebUIError(Exception)`, with a human-readable message.
  - `class OpenWebUIClient(base_url: str, api_key: str, timeout: float = 30.0)`
  - `async def list_knowledge(self) -> list[dict]`. Each dict has `id`, `name`, `description`.
  - `async def query(self, collection_names: list[str], query: str, k: int | None) -> list[dict]`. Each dict has `content`, `source`, `score`, sorted by score descending.

- [ ] **Step 1: Write failing tests** (use respx to mock httpx):
  - `test_list_paginates`: page 1 returns 30 items with `total=35`, and page 2 returns 5. Assert `len(result) == 35`.
  - `test_list_accepts_bare_list`: the response is a bare JSON list. Assert the items are returned.
  - `test_query_flattens`: the response is `{documents:[["a","b"]], metadatas:[[{"name":"f1.pdf"},{"source":"f2"}]], distances:[[0.4,0.9]]}`. Assert the result is `[{"content":"b","source":"f2","score":0.9},{"content":"a","source":"f1.pdf","score":0.4}]`. Source fallback order: `name`, then `source`, then `"unknown"`.
  - `test_query_omits_k_when_none`: assert the request JSON has no `k` key.
  - `test_bearer_header_sent`: assert `Authorization: Bearer sk-test`.
  - `test_401_maps_to_auth_error`: 401 → `OpenWebUIError` whose message contains "API key".
  - `test_403_maps_to_access_error`: 403 and 404 → message contains "access".
- [ ] **Step 2: Run tests and confirm they fail.** Run `cd mcp-servers/openwebui-rag && pip install -e .[dev] && pytest tests/test_client.py -v`. Expected: ImportError.
- [ ] **Step 3: Implement `client.py`.** Paginate until the collected count reaches `total`, or a page comes back empty.
- [ ] **Step 4: Run the tests again.** Expected: all pass.
- [ ] **Step 5: Commit** with message `feat(mcp): Open WebUI API client`.

### Task 2: MCP server and tools

**Files:**
- Create: `mcp-servers/openwebui-rag/src/openwebui_rag_mcp/server.py`
- Test: `mcp-servers/openwebui-rag/tests/test_server.py`

**Interfaces:**
- Consumes `OpenWebUIClient` and `OpenWebUIError` from Task 1.
- Produces:
  - `async def list_knowledge_bases(ctx) -> str`
  - `async def search_knowledge(query: str, knowledge_ids: list[str] | None = None, k: int | None = None, ctx=None) -> str`
  - `def main() -> None`
  - `def get_api_key(ctx) -> str`: in HTTP mode, the Bearer token from the request headers (`ctx.request_context.request.headers`). Otherwise `OPENWEBUI_API_KEY`. If neither is present, raise `OpenWebUIError("No API key …")`.

- [ ] **Step 1: Write failing tests.** Patch `OpenWebUIClient` with a fake.
  - `test_list_formats`: the output contains each id and name.
  - `test_search_formats`: each hit is rendered as `[n] source (score 0.90)\ncontent`.
  - `test_search_all_uses_accessible_ids`: with `knowledge_ids=None`, the client's `query` is called with every id from `list_knowledge`.
  - `test_search_all_exceeds_cap`: `MAX_COLLECTIONS=2` and 3 collections → the tool raises an error mentioning `knowledge_ids`.
  - `test_search_empty`: `query` returns `[]` → `"No results."`.
  - `test_http_mode_uses_header_key`: a fake ctx whose headers carry `Authorization: Bearer sk-user` → `get_api_key` returns `sk-user`.
- [ ] **Step 2: Run the tests and confirm they fail.**
- [ ] **Step 3: Implement `server.py`.** Use FastMCP `"openwebui-rag"`. Tool descriptions explain the intended order: call list first, then search. In `main()`, branch on `MCP_TRANSPORT`: `mcp.run()` for stdio, `mcp.run(transport="streamable-http")` with host and port set via `mcp.settings` for http. Raise `OpenWebUIError` messages as tool errors.
- [ ] **Step 4: Run the tests again and confirm they pass.**
- [ ] **Step 5: Manual check.** Run `OPENWEBUI_URL=http://localhost:8080 OPENWEBUI_API_KEY=sk-… npx @modelcontextprotocol/inspector openwebui-rag-mcp`. Expected: both tools are listed, and a search returns chunks.
- [ ] **Step 6: Commit** with message `feat(mcp): RAG search tools`.

### Task 3: Docker and README

**Files:**
- Create: `mcp-servers/openwebui-rag/Dockerfile`
- Create: `mcp-servers/openwebui-rag/.dockerignore`
- Create: `mcp-servers/openwebui-rag/README.md` (in Korean)

- [ ] **Step 1: Write the Dockerfile.** Base image `python:3.11-slim`, `pip install .`, a non-root user, `ENV MCP_TRANSPORT=http MCP_PORT=8000`, `EXPOSE 8000`, `CMD ["openwebui-rag-mcp"]`.
- [ ] **Step 2: Build and run.** Run `docker build -t openwebui-rag-mcp mcp-servers/openwebui-rag && docker run --rm -p 8000:8000 -e OPENWEBUI_URL=http://host.docker.internal:8080 openwebui-rag-mcp`. Expected: the server listens on `/mcp`. An MCP initialize request via Inspector (Streamable HTTP, Bearer header) succeeds.
- [ ] **Step 3: Write the README.** Cover these sections:
  - What the server does and its limits: search settings only, no query rewriting or full-context mode.
  - Admin checklist: `ENABLE_API_KEYS` on; if endpoint restrictions are used, allow `/api/v1/knowledge` and `/api/v1/retrieval/query/collection`; users can create keys.
  - Env var table.
  - stdio registration examples for Claude Desktop (`claude_desktop_config.json`), Claude Code (`claude mcp add`), and Cursor (`.cursor/mcp.json`).
  - Container run with `docker run`, plus a docker-compose example that sits alongside the `open-webui` service (`OPENWEBUI_URL=http://open-webui:8080`).
  - HTTP registration with a Bearer header, for internal use only.
- [ ] **Step 4: Commit** with message `feat(mcp): Dockerfile and README`.
