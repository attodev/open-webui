# Open WebUI RAG MCP (newapi key auth) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A second MCP server, `mcp-servers/openwebui-rag-newapi/`. Users authenticate with only their newapi API key. The server trades that key for a newapi OIDC access token, exchanges it for an Open WebUI JWT, and searches with the JWT.

**Architecture:** Copy of `mcp-servers/openwebui-rag/` (same tools, output format and transports), with a new `auth.py` (newapi token fetch, Open WebUI token exchange, JWT cache). The client takes a JWT provider instead of a static key.

**Tech Stack:** Python ≥3.11, `mcp>=2.3,<3` (`mcp.server.mcpserver.MCPServer`), `httpx`, pytest, pytest-asyncio, respx, Docker.

**Spec:** `docs/superpowers/specs/2026-10-31-openwebui-rag-mcp-newapi-design.md`. Contract with newapi: `docs/superpowers/specs/2026-10-31-newapi-key-to-oidc-token-requirements.md`.

## Global Constraints

- Do not modify `mcp-servers/openwebui-rag/`, `backend/`, or `src/`.
- Env vars:
  - `OPENWEBUI_URL` (required)
  - `NEWAPI_URL` (required)
  - `NEWAPI_TOKEN_PATH` (default `/api/oidc/token-from-key`)
  - `OPENWEBUI_OAUTH_PROVIDER` (default `oidc`)
  - `NEWAPI_API_KEY` (stdio only; HTTP mode requires the `Authorization: Bearer` header with no env fallback)
  - `MAX_COLLECTIONS` (default `20`)
  - `MCP_TRANSPORT` (`stdio` | `http`), `MCP_HOST` (`0.0.0.0`), `MCP_PORT` (`8000`)
- newapi call: `POST {NEWAPI_URL}{NEWAPI_TOKEN_PATH}` with `Authorization: Bearer <sk-key>` → `{access_token, token_type, expires_in}`. Errors are `{error, message}` with 401, 403 or 429.
- Exchange call: `POST {OPENWEBUI_URL}/api/v1/auths/oauth/{provider}/token/exchange` with JSON `{"token": access_token}` → on success JSON with `token` and optional `expires_at` (epoch seconds, may be null). A 200 without `token` means pending or MFA.
- Never log or store raw keys or tokens. The cache key is `sha256(key)`.
- Error messages are copied verbatim from the spec section "내가 정한 사항".

## Review Focus

- Concurrent first requests with the same key must trigger exactly one exchange. Test: `test_concurrent_single_exchange`.
- When Open WebUI returns 401 on a cached JWT, the server re-exchanges once and retries. If the retry also gets 401, it surfaces an error and does not loop. Test: `test_401_reexchange_once`.
- A 200 exchange without a `token` is not cached and returns the pending/MFA message. Test: `test_exchange_no_token`.
- Exchange 403s are told apart by `detail`: "User not found" vs `ACCESS_PROHIBITED` vs "Token exchange is disabled". Test: `test_exchange_403_variants`.
- A raw key never appears in error messages or the repr of the cache. Test: `test_no_secret_leak`.

---

### Task 1: Package copy + auth module

**Files:**
- Create: `mcp-servers/openwebui-rag-newapi/` by copying the package skeleton and client from `mcp-servers/openwebui-rag/`. Rename the package to `openwebui_rag_mcp_newapi` and the script to `openwebui-rag-mcp-newapi`.
- Create: `src/openwebui_rag_mcp_newapi/auth.py`
- Modify (in the copy): `client.py`. `OpenWebUIClient(base_url, token_provider, timeout=30.0)`, where `token_provider` is an object with `async get_token() -> str` and `async invalidate() -> None`. On a 401 it calls `invalidate()` and retries once.
- Test: `tests/test_auth.py`, plus the copied `tests/test_client.py` adapted to the new constructor

**Interfaces (produced):**
- `class AuthError(OpenWebUIError)`. Reuse the copied `OpenWebUIError`.
- `class TokenBroker(openwebui_url: str, newapi_url: str, newapi_token_path: str, provider: str, timeout: float = 30.0, max_entries: int = 1000)`
  - `async get_jwt(api_key: str) -> str`: the cached JWT, refreshed 60 s before expiry. TTL comes from `expires_at`; if that is null, 3600 s.
  - `async invalidate(api_key: str) -> None`
  - `for_key(api_key: str) -> TokenProvider`: an adapter that satisfies the client's `token_provider`.

- [ ] **Step 1: Write failing tests.** Use respx with a frozen or monkeypatched clock.
  - `test_fetch_and_exchange`: newapi receives `Bearer sk-a`, and the exchange body is `{"token": "at"}` sent to `/api/v1/auths/oauth/oidc/token/exchange`. Returns the JWT.
  - `test_cache_reuse`: a second call makes no HTTP requests.
  - `test_refresh_before_expiry`: `expires_at=now+50` triggers a re-exchange on the next call.
  - `test_null_expires_at_uses_3600`
  - `test_concurrent_single_exchange`: 10 concurrent `get_jwt("sk-a")` calls produce one newapi call and one exchange call.
  - `test_newapi_errors`: 401, 403, 429 and 500 each map to the spec message.
  - `test_exchange_no_token`
  - `test_exchange_403_variants`
  - `test_exchange_400_404_429`
  - `test_no_secret_leak`
  - `test_eviction_max_entries`
  - In the client tests, `test_401_reexchange_once`.
- [ ] **Step 2: Run the tests and confirm they fail.**
- [ ] **Step 3: Implement.** Use one `asyncio.Lock` per cache key and an `OrderedDict` for eviction.
- [ ] **Step 4: Run the tests and confirm they pass.** The full suite must pass.
- [ ] **Step 5: Commit** with message `feat(mcp-newapi): token broker and client`.

### Task 2: Server, Docker, README, end-to-end

**Files:**
- Modify (copy): `server.py`. `get_api_key(ctx)` reads `NEWAPI_API_KEY` in stdio mode and only the Bearer header in http mode. The tools build the client with `broker.for_key(key)`. There is one module-level `TokenBroker`, built from env.
- Copy and adapt: `Dockerfile` (script `openwebui-rag-mcp-newapi`), `.dockerignore`, `tests/test_server.py`
- Create: `README.md` in Korean, covering:
  - the difference from the original (only a newapi key is registered)
  - Open WebUI admin setup: `ENABLE_OAUTH_TOKEN_EXCHANGE=true`, recommended `OAUTH_TOKEN_EXCHANGE_TRUSTED_CLIENT_IDS`, and the IP-based `OAUTH_TOKEN_EXCHANGE_RATE_LIMIT` caveat
  - `OPENWEBUI_OAUTH_PROVIDER` must match the registered provider name
  - users must sign in to the Open WebUI web UI once
  - the dependency on the newapi endpoint, with a link to the requirements doc
  - an env table
  - Claude Desktop, Claude Code and Cursor examples
  - docker run and compose with `127.0.0.1:8000:8000`

- [ ] **Step 1: Adapt the server tests.** Include `test_http_mode_no_env_fallback`, which sets `NEWAPI_API_KEY` in http mode with no header and expects an error.
- [ ] **Step 2: Implement and run the tests until they pass.**
- [ ] **Step 3: End-to-end.**
  - Write a stub script in the scratchpad that serves:
    - newapi `POST /api/oidc/token-from-key`: `sk-good` → `{"access_token":"at-1","expires_in":600}`, otherwise 401
    - Open WebUI: token exchange `at-1` → `{"token":"jwt-1","expires_at":null}`, the knowledge list, and query endpoints that require `Bearer jwt-1`
  - Build the image and run it with `--network host` in http mode.
  - With the SDK client, check:
    - `Bearer sk-good`: both tools succeed, and the stub logs one exchange across two calls (cache).
    - `Bearer sk-bad`: the newapi-invalid-key message.
  - Remove the container and stub afterwards. Kill processes by PID, not by `pkill -f`.
- [ ] **Step 4: Commit** with message `feat(mcp-newapi): server, Docker and README`.
