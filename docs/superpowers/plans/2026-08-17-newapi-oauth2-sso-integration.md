# new-api OAuth2 SSO Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make OpenWebUI authenticate exclusively through new-api's IdP-initiated OAuth2 hand-off, and route per-user LLM billing through that same token, per `docs/superpowers/specs/2026-08-17-newapi-oauth2-sso-integration-design.md`.

**Architecture:** A new, isolated router (`routers/newapi_sso.py`) receives an authorization code new-api already generated, exchanges it server-to-server for an access token, provisions/logs in the local user via the existing session-creation helper, and stores the token in the existing `OAuthSessions` table. A new `auth_type` branch in the OpenAI connection layer (`routers/openai.py`) resolves that stored token per request instead of a fixed admin key. Feature removal is achieved almost entirely by setting existing config flags (`ENABLE_PASSWORD_AUTH`, `ui.enable_login_form`, `ui.enable_signup`, `auth.enable_api_keys`, `ui.enable_password_change_form`) to `false` — only user management (add/delete) and the login page's new-api entry point need new UI code.

**Tech Stack:** FastAPI, SQLAlchemy (async), aiohttp (outbound HTTP), pytest + pytest-asyncio + httpx `ASGITransport` (new backend test harness — none currently exists in this repo), Svelte 5 frontend.

## Global Constraints

- No refresh token exists on the new-api side — on expiry/logout there is only "get a fresh code from new-api," never a silent refresh (spec §3, §"new-api 팀 의존성").
- The authorization `code` is single-use with a 120s TTL; never retry a failed exchange with the same code (spec "에러 처리 및 엣지 케이스").
- The OpenWebUI session issued from this flow must be a fixed 24h expiry, independent of the global `auth.jwt_expiry` value, and must not slide/renew on activity (spec "Section A/B 정정" and "세션 상한 24h의 근거").
- The callback must never trust pre-existing cookies on the incoming request — identity comes only from the freshly exchanged token (spec "컴포넌트 상세 > 1. 신규 라우트").
- No domain allowlist on email (spec "이메일 도메인 제한" decision: 무제한 허용).
- No break-glass path for local password sign-in once `ENABLE_NEWAPI_SSO` is set — `ENABLE_PASSWORD_AUTH=False` is a deployment-time decision with no runtime override (spec "결정" callout).
- All new env vars follow this repo's `os.getenv(...)` convention in `backend/open_webui/env.py`, not the runtime `Config` table (spec "설정값").

---

## File Structure

| File | Change |
|---|---|
| `backend/open_webui/test/__init__.py` | new, empty |
| `backend/open_webui/test/conftest.py` | new — pytest fixtures: temp SQLite DB, ASGI test client |
| `backend/open_webui/routers/auths.py` | modify `create_session_response` to accept an `expires_delta` override |
| `backend/open_webui/utils/newapi_oauth.py` | new — `exchange_code_for_token`, `fetch_userinfo`, env-driven config |
| `backend/open_webui/env.py` | add `ENABLE_NEWAPI_SSO`, `NEWAPI_OAUTH_BASE_URL`, `NEWAPI_OAUTH_CLIENT_ID`, `NEWAPI_OAUTH_CLIENT_SECRET`, `NEWAPI_ENTRY_URL` |
| `backend/open_webui/routers/newapi_sso.py` | new — `GET /auth/newapi/callback` |
| `backend/open_webui/main.py` | register `newapi_sso.router`; add `newapi_sso` and `enable_user_management` fields to `get_app_config()` |
| `backend/open_webui/routers/openai.py` | `get_headers_and_cookies`: new `auth_type == 'newapi_session'` branch |
| `backend/open_webui/config.py` | add `'ui.enable_user_management': True` to `DEFAULT_CONFIG` |
| `src/lib/components/admin/Users/UserList.svelte` | gate Add User button + per-row Delete button behind `$config.features.enable_user_management` |
| `src/routes/auth/+page.svelte` | render a new-api entry button when SSO is enabled |
| `src/lib/components/chat/Messages/ResponseMessage.svelte` | special-case the `NEWAPI_RECONNECT_REQUIRED` error marker |

Every task below that touches the backend depends on **Task 1** (test harness) having landed first.

---

### Task 1: Backend test harness

No `test_*.py` files exist anywhere in this repo today, so this task creates the minimum pytest setup the rest of the plan needs: an isolated temp-file SQLite database per test function, and an `httpx.AsyncClient` wired directly to a FastAPI app via `ASGITransport` (no running server, no port).

**Files:**
- Create: `backend/open_webui/test/__init__.py`
- Create: `backend/open_webui/test/conftest.py`
- Test: `backend/open_webui/test/test_harness_smoke.py`

**Interfaces:**
- Produces: `db_engine` fixture (sync SQLAlchemy `Engine`, schema created via `Base.metadata.create_all`), `async_client` fixture (`httpx.AsyncClient`, base_url `http://test`), both consumed by every later backend task's tests.

- [ ] **Step 1: Write the failing smoke test**

```python
# backend/open_webui/test/test_harness_smoke.py
import pytest


@pytest.mark.asyncio
async def test_harness_can_reach_a_bare_route(async_client):
    from fastapi import FastAPI

    app = FastAPI()

    @app.get('/ping')
    async def ping():
        return {'ok': True}

    async_client._transport = async_client._transport.__class__(app=app)
    res = await async_client.get('/ping')
    assert res.status_code == 200
    assert res.json() == {'ok': True}
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd backend && pytest open_webui/test/test_harness_smoke.py -v`
Expected: FAIL — `fixture 'async_client' not found` (conftest.py does not exist yet).

- [ ] **Step 3: Create `backend/open_webui/test/__init__.py`**

```python
```

(Empty file — makes `open_webui.test` an importable package so test modules can use relative imports later if needed.)

- [ ] **Step 4: Write `conftest.py`**

```python
# backend/open_webui/test/conftest.py
"""
Shared pytest fixtures for the backend test suite.

IMPORTANT: the env vars below must be set before any `open_webui.*` module
is imported, because `open_webui.config` reads them at import time (and can
trigger an Alembic migration run or a hard SystemExit if WEBUI_SECRET_KEY is
missing). conftest.py module-level code runs before pytest collects any
test module in this directory, which is what makes this reliable.
"""

import os
import tempfile

os.environ.setdefault('WEBUI_SECRET_KEY', 'test-secret-key-not-for-production')
os.environ.setdefault('WEBUI_AUTH', 'True')
os.environ.setdefault('ENABLE_DB_MIGRATIONS', 'False')  # schema created directly from ORM metadata instead

_TMP_DB_DIR = tempfile.mkdtemp(prefix='openwebui-test-db-')
os.environ.setdefault('DATABASE_URL', f'sqlite:///{_TMP_DB_DIR}/test.db')

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from open_webui.internal.db import Base, engine


@pytest.fixture(scope='function')
def db_engine():
    """Create every ORM table fresh for each test function, then drop them."""
    Base.metadata.create_all(bind=engine)
    yield engine
    Base.metadata.drop_all(bind=engine)


@pytest_asyncio.fixture
async def async_client(db_engine):
    """An httpx.AsyncClient with no app wired in yet.

    Each test module replaces `async_client._transport` with an
    `ASGITransport(app=<app under test>)` before making requests — see
    test_harness_smoke.py for the pattern. Tasks 4-8 build on this same
    fixture rather than importing the full `open_webui.main:app` (which
    would trigger the full startup lifespan, license fetch, and static-dir
    sync — unnecessary for router-level tests).
    """
    async with AsyncClient(transport=ASGITransport(app=None), base_url='http://test') as client:
        yield client
```

- [ ] **Step 5: Run the smoke test again**

Run: `cd backend && pytest open_webui/test/test_harness_smoke.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add backend/open_webui/test/__init__.py backend/open_webui/test/conftest.py backend/open_webui/test/test_harness_smoke.py
git commit -m "test: add backend pytest harness (temp SQLite DB + ASGI test client)"
```

---

### Task 2: `create_session_response` accepts an `expires_delta` override

**Files:**
- Modify: `backend/open_webui/routers/auths.py:164-215` (the `create_session_response` function)
- Test: `backend/open_webui/test/routers/test_auths_session_response.py`

**Interfaces:**
- Consumes: `open_webui.internal.db.get_async_session` (via a raw `AsyncSessionLocal`), `open_webui.models.users.Users.insert_new_user`.
- Produces: `create_session_response(request, user, db, response=None, set_cookie=False, source='api', expires_delta: datetime.timedelta | None = None) -> dict`. When `expires_delta` is passed, it takes priority over `Config.get('auth.jwt_expiry')`. Task 5 relies on this exact parameter name and behavior.

- [ ] **Step 1: Write the failing test**

```python
# backend/open_webui/test/routers/__init__.py
```
```python
# backend/open_webui/test/routers/test_auths_session_response.py
import datetime
import time

import pytest
from open_webui.models.users import Users


@pytest.mark.asyncio
async def test_create_session_response_honors_expires_delta_override(db_engine):
    from fastapi import Request
    from open_webui.internal.db import AsyncSessionLocal
    from open_webui.routers.auths import create_session_response
    from open_webui.utils.auth import decode_token

    async with AsyncSessionLocal() as db:
        user = await Users.insert_new_user(
            id='user-1', name='Test User', email='test@example.com', role='user', db=db
        )

        scope = {'type': 'http', 'method': 'GET', 'path': '/', 'headers': []}
        request = Request(scope)

        before = int(time.time())
        result = await create_session_response(
            request, user, db, expires_delta=datetime.timedelta(hours=24)
        )

        decoded = decode_token(result['token'])
        assert decoded['exp'] - before == pytest.approx(24 * 3600, abs=5)
        assert result['expires_at'] - before == pytest.approx(24 * 3600, abs=5)
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd backend && pytest open_webui/test/routers/test_auths_session_response.py -v`
Expected: FAIL — `TypeError: create_session_response() got an unexpected keyword argument 'expires_delta'`

- [ ] **Step 3: Modify `create_session_response`**

In `backend/open_webui/routers/auths.py`, replace the existing function (currently at lines 164-215):

```python
async def create_session_response(
    request: Request,
    user,
    db,
    response: Response = None,
    set_cookie: bool = False,
    source: str = 'api',
    expires_delta: datetime.timedelta | None = None,
) -> dict:
    """
    Create JWT token and build session response for a user.
    Shared helper for signin, signup, ldap_auth, add_user, token_exchange,
    and the new-api SSO callback.

    Args:
        request: FastAPI request object
        user: User object
        db: Database session
        response: FastAPI response object (required if set_cookie is True)
        set_cookie: Whether to set the auth cookie on the response
        expires_delta: When provided, overrides Config('auth.jwt_expiry') for
            this call only. Used by the new-api SSO callback to force a
            fixed 24h session regardless of the deployment's global setting
            (see docs/superpowers/specs/2026-08-17-newapi-oauth2-sso-integration-design.md).
    """
    if expires_delta is None:
        expires_delta = parse_duration(await Config.get('auth.jwt_expiry'))
    expires_at = None
    if expires_delta:
        expires_at = int(time.time()) + int(expires_delta.total_seconds())

    token = create_token(
        data={'id': user.id},
        expires_delta=expires_delta,
    )

    if set_cookie and response:
        datetime_expires_at = datetime.datetime.fromtimestamp(expires_at, datetime.timezone.utc) if expires_at else None
        max_age = int(expires_delta.total_seconds()) if expires_delta else None
        response.set_cookie(
            key='token',
            value=token,
            expires=datetime_expires_at,
            httponly=True,
            samesite=WEBUI_AUTH_COOKIE_SAME_SITE,
            secure=WEBUI_AUTH_COOKIE_SECURE,
            **({'max_age': max_age} if max_age is not None else {}),
        )

    user_permissions = await get_permissions(user.id, await Config.get('user.permissions'), db=db)
    await publish_event(
        request,
        EVENTS.AUTH_LOGIN,
        actor=user,
        subject_id=user.id,
        subject_type='user',
        source=source,
        data={'auth_method': source},
    )

    return {
        'token': token,
        'token_type': 'Bearer',
        'expires_at': expires_at,
        'id': user.id,
        'email': user.email,
        'name': user.name,
        'role': user.role,
        'profile_image_url': f'/api/v1/users/{user.id}/profile/image',
        'permissions': user_permissions,
    }
```

(Only the signature and the first four lines of the body changed — everything else is unchanged from the current implementation.)

- [ ] **Step 4: Run the test again**

Run: `cd backend && pytest open_webui/test/routers/test_auths_session_response.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/open_webui/routers/auths.py backend/open_webui/test/routers/__init__.py backend/open_webui/test/routers/test_auths_session_response.py
git commit -m "feat: allow create_session_response to override the JWT expiry"
```

---

### Task 3: new-api OAuth2 client helper (`utils/newapi_oauth.py`)

Isolates the two outbound HTTP calls (token exchange, userinfo) so they can be unit-tested without a real new-api server, and so the router (Task 4) stays focused on orchestration.

**Files:**
- Modify: `backend/open_webui/env.py` (add new env vars, near `WEBUI_AUTH_TRUSTED_EMAIL_HEADER` at line 756)
- Create: `backend/open_webui/utils/newapi_oauth.py`
- Test: `backend/open_webui/test/utils/test_newapi_oauth.py`

**Interfaces:**
- Produces:
  - `NewapiOAuthError(Exception)` — raised with a `.reason: str` attribute, one of `'invalid_grant'`, `'invalid_client'`, `'network_error'`, `'invalid_userinfo'`.
  - `async def exchange_code_for_token(code: str) -> dict` — returns `{'access_token': str, 'expires_in': int}` on success, raises `NewapiOAuthError` otherwise.
  - `async def fetch_userinfo(access_token: str) -> dict` — returns `{'sub': str, 'email': str, 'name': str, 'is_admin': bool}` (`is_admin` defaults to `False` if the claim is absent — new-api has not shipped it yet per the design spec), raises `NewapiOAuthError` on failure or missing `sub`/`email`.
- Consumes: `open_webui.utils.session_pool.get_session`, `open_webui.utils.session_pool.cleanup_response`.

- [ ] **Step 1: Add env vars**

In `backend/open_webui/env.py`, immediately after the `WEBUI_AUTH_TRUSTED_ROLE_HEADER` line (line 758):

```python
ENABLE_NEWAPI_SSO = os.getenv('ENABLE_NEWAPI_SSO', 'False').lower() == 'true'
NEWAPI_OAUTH_BASE_URL = os.getenv('NEWAPI_OAUTH_BASE_URL', '').rstrip('/')
NEWAPI_OAUTH_CLIENT_ID = os.getenv('NEWAPI_OAUTH_CLIENT_ID', '')
NEWAPI_OAUTH_CLIENT_SECRET = os.getenv('NEWAPI_OAUTH_CLIENT_SECRET', '')
NEWAPI_ENTRY_URL = os.getenv('NEWAPI_ENTRY_URL', '')
```

- [ ] **Step 2: Write the failing tests**

```python
# backend/open_webui/test/utils/__init__.py
```
```python
# backend/open_webui/test/utils/test_newapi_oauth.py
from unittest.mock import AsyncMock, patch

import pytest


class _FakeResponse:
    def __init__(self, status, payload):
        self.status = status
        self._payload = payload

    async def json(self, **kwargs):
        return self._payload


class _FakeSession:
    def __init__(self, response):
        self._response = response

    def post(self, *args, **kwargs):
        return _CtxManager(self._response)

    def get(self, *args, **kwargs):
        return _CtxManager(self._response)


class _CtxManager:
    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self._response

    async def __aexit__(self, *exc):
        return False


@pytest.mark.asyncio
async def test_exchange_code_for_token_success():
    from open_webui.utils.newapi_oauth import exchange_code_for_token

    fake_session = _FakeSession(_FakeResponse(200, {'access_token': 'sk-abc', 'token_type': 'Bearer', 'expires_in': 86400}))
    with patch('open_webui.utils.newapi_oauth.get_session', new=AsyncMock(return_value=fake_session)):
        result = await exchange_code_for_token('a-code')

    assert result == {'access_token': 'sk-abc', 'expires_in': 86400}


@pytest.mark.asyncio
async def test_exchange_code_for_token_invalid_grant():
    from open_webui.utils.newapi_oauth import NewapiOAuthError, exchange_code_for_token

    fake_session = _FakeSession(_FakeResponse(400, {'error': 'invalid_grant'}))
    with patch('open_webui.utils.newapi_oauth.get_session', new=AsyncMock(return_value=fake_session)):
        with pytest.raises(NewapiOAuthError) as exc_info:
            await exchange_code_for_token('a-code')

    assert exc_info.value.reason == 'invalid_grant'


@pytest.mark.asyncio
async def test_exchange_code_for_token_invalid_client():
    from open_webui.utils.newapi_oauth import NewapiOAuthError, exchange_code_for_token

    fake_session = _FakeSession(_FakeResponse(401, {'error': 'invalid_client'}))
    with patch('open_webui.utils.newapi_oauth.get_session', new=AsyncMock(return_value=fake_session)):
        with pytest.raises(NewapiOAuthError) as exc_info:
            await exchange_code_for_token('a-code')

    assert exc_info.value.reason == 'invalid_client'


@pytest.mark.asyncio
async def test_exchange_code_for_token_network_error():
    from open_webui.utils.newapi_oauth import NewapiOAuthError, exchange_code_for_token

    async def _raise_session():
        import aiohttp

        raise aiohttp.ClientConnectionError('boom')

    with patch('open_webui.utils.newapi_oauth.get_session', new=_raise_session):
        with pytest.raises(NewapiOAuthError) as exc_info:
            await exchange_code_for_token('a-code')

    assert exc_info.value.reason == 'network_error'


@pytest.mark.asyncio
async def test_fetch_userinfo_success_defaults_is_admin_false():
    from open_webui.utils.newapi_oauth import fetch_userinfo

    fake_session = _FakeSession(_FakeResponse(200, {'sub': 'u1', 'email': 'a@b.com', 'name': 'A'}))
    with patch('open_webui.utils.newapi_oauth.get_session', new=AsyncMock(return_value=fake_session)):
        result = await fetch_userinfo('sk-abc')

    assert result == {'sub': 'u1', 'email': 'a@b.com', 'name': 'A', 'is_admin': False}


@pytest.mark.asyncio
async def test_fetch_userinfo_missing_sub_raises():
    from open_webui.utils.newapi_oauth import NewapiOAuthError, fetch_userinfo

    fake_session = _FakeSession(_FakeResponse(200, {'email': 'a@b.com', 'name': 'A'}))
    with patch('open_webui.utils.newapi_oauth.get_session', new=AsyncMock(return_value=fake_session)):
        with pytest.raises(NewapiOAuthError) as exc_info:
            await fetch_userinfo('sk-abc')

    assert exc_info.value.reason == 'invalid_userinfo'
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `cd backend && pytest open_webui/test/utils/test_newapi_oauth.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'open_webui.utils.newapi_oauth'`

- [ ] **Step 4: Write the implementation**

```python
# backend/open_webui/utils/newapi_oauth.py
"""
Server-to-server client for new-api's OAuth2 provider.

Required deployment env vars (set to '' / False by default, meaning SSO is
off until configured):
    ENABLE_NEWAPI_SSO=True
    NEWAPI_OAUTH_BASE_URL=https://<newapi-host>
    NEWAPI_OAUTH_CLIENT_ID=<provided by new-api ops>
    NEWAPI_OAUTH_CLIENT_SECRET=<provided by new-api ops>
    NEWAPI_ENTRY_URL=<URL shown to users to re-enter new-api>

See docs/superpowers/specs/2026-08-17-newapi-oauth2-sso-integration-design.md
and docs/2026-08-17-openwebui-response-to-newapi-oauth2-handoff.md for the
full protocol contract this module implements against.
"""

import logging

import aiohttp
from open_webui.env import NEWAPI_OAUTH_BASE_URL, NEWAPI_OAUTH_CLIENT_ID, NEWAPI_OAUTH_CLIENT_SECRET
from open_webui.utils.session_pool import cleanup_response, get_session

log = logging.getLogger(__name__)


class NewapiOAuthError(Exception):
    def __init__(self, reason: str, message: str | None = None):
        self.reason = reason
        super().__init__(message or reason)


async def exchange_code_for_token(code: str) -> dict:
    """POST /oauth2/token. Returns {'access_token': str, 'expires_in': int}."""
    session = await get_session()
    try:
        async with session.post(
            f'{NEWAPI_OAUTH_BASE_URL}/oauth2/token',
            data={
                'grant_type': 'authorization_code',
                'code': code,
                'client_id': NEWAPI_OAUTH_CLIENT_ID,
                'client_secret': NEWAPI_OAUTH_CLIENT_SECRET,
            },
        ) as response:
            payload = await response.json()
            if response.status == 400 and payload.get('error') == 'invalid_grant':
                raise NewapiOAuthError('invalid_grant', 'new-api rejected the authorization code')
            if response.status == 401 and payload.get('error') == 'invalid_client':
                log.error('new-api token exchange failed: invalid_client (check NEWAPI_OAUTH_CLIENT_ID/SECRET)')
                raise NewapiOAuthError('invalid_client', 'OpenWebUI is misconfigured for new-api SSO')
            if response.status != 200:
                log.error('Unexpected new-api token exchange response: %s %s', response.status, payload)
                raise NewapiOAuthError('network_error', f'Unexpected response status {response.status}')
            return {'access_token': payload['access_token'], 'expires_in': payload['expires_in']}
    except aiohttp.ClientError as e:
        log.error('Network error exchanging new-api authorization code: %s', e)
        raise NewapiOAuthError('network_error', str(e)) from e


async def fetch_userinfo(access_token: str) -> dict:
    """GET /oauth2/userinfo. Returns {'sub', 'email', 'name', 'is_admin'}."""
    session = await get_session()
    try:
        async with session.get(
            f'{NEWAPI_OAUTH_BASE_URL}/oauth2/userinfo',
            headers={'Authorization': f'Bearer {access_token}'},
        ) as response:
            payload = await response.json()
            if response.status != 200:
                log.error('Unexpected new-api userinfo response: %s %s', response.status, payload)
                raise NewapiOAuthError('network_error', f'Unexpected response status {response.status}')
            sub = payload.get('sub')
            email = payload.get('email')
            if not sub or not email:
                log.error("new-api userinfo response missing 'sub' or 'email': %s", payload)
                raise NewapiOAuthError('invalid_userinfo', "Response missing 'sub' or 'email'")
            return {
                'sub': sub,
                'email': email,
                'name': payload.get('name') or email,
                'is_admin': bool(payload.get('is_admin', False)),
            }
    except aiohttp.ClientError as e:
        log.error('Network error fetching new-api userinfo: %s', e)
        raise NewapiOAuthError('network_error', str(e)) from e
```

- [ ] **Step 5: Run the tests again**

Run: `cd backend && pytest open_webui/test/utils/test_newapi_oauth.py -v`
Expected: PASS (7 tests)

- [ ] **Step 6: Commit**

```bash
git add backend/open_webui/env.py backend/open_webui/utils/newapi_oauth.py backend/open_webui/test/utils/__init__.py backend/open_webui/test/utils/test_newapi_oauth.py
git commit -m "feat: add new-api OAuth2 client helper (token exchange + userinfo)"
```

---

### Task 4: `/auth/newapi/callback` — new-user happy path

**Files:**
- Create: `backend/open_webui/routers/newapi_sso.py`
- Test: `backend/open_webui/test/routers/test_newapi_sso.py`

**Interfaces:**
- Consumes: `exchange_code_for_token`, `fetch_userinfo` (Task 3); `create_session_response` with `expires_delta` (Task 2); `open_webui.models.users.Users.get_user_by_oauth_sub`, `.get_user_by_email`, `.update_user_oauth_by_id`, `.get_num_users`, `.update_user_role_by_id`, `.get_user_by_id`; `open_webui.models.auths.Auths.insert_new_auth`; `open_webui.models.oauth_sessions.OAuthSessions.get_session_by_provider_and_user_id`, `.create_session`, `.update_session_by_id`; `open_webui.utils.groups.apply_default_group_assignment`.
- Produces: `router = APIRouter()` with `GET /callback` (mounted at `/auth/newapi` by Task 7), and `NEWAPI_SESSION_TTL = datetime.timedelta(hours=24)` (a module constant Task 8 does **not** need, but which documents the Global Constraint in code).

- [ ] **Step 1: Write the failing test**

```python
# backend/open_webui/test/routers/test_newapi_sso.py
import time
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport
from open_webui.internal.db import AsyncSessionLocal
from open_webui.models.users import Users


def _mount(app):
    from open_webui.routers import newapi_sso

    app.include_router(newapi_sso.router, prefix='/auth/newapi')


@pytest.mark.asyncio
async def test_callback_creates_new_user_and_stores_token(db_engine, async_client):
    app = FastAPI()
    _mount(app)
    async_client._transport = ASGITransport(app=app)

    with (
        patch(
            'open_webui.routers.newapi_sso.exchange_code_for_token',
            new=AsyncMock(return_value={'access_token': 'sk-abc', 'expires_in': 86400}),
        ),
        patch(
            'open_webui.routers.newapi_sso.fetch_userinfo',
            new=AsyncMock(
                return_value={'sub': 'newapi-user-1', 'email': 'alice@example.com', 'name': 'Alice', 'is_admin': False}
            ),
        ),
    ):
        res = await async_client.get('/auth/newapi/callback?code=abc123&state=xyz', follow_redirects=False)

    assert res.status_code == 302
    assert res.headers['location'] == '/'
    assert 'token' in res.cookies

    async with AsyncSessionLocal() as db:
        user = await Users.get_user_by_email('alice@example.com', db=db)
        assert user is not None
        assert user.oauth == {'newapi': {'sub': 'newapi-user-1'}}
        # First user through this flow becomes admin (spec: first-login bootstrap).
        assert user.role == 'admin'

    from open_webui.models.oauth_sessions import OAuthSessions

    async with AsyncSessionLocal() as db:
        session = await OAuthSessions.get_session_by_provider_and_user_id('newapi', user.id, db=db)
        assert session is not None
        assert session.token['access_token'] == 'sk-abc'
        assert session.expires_at - int(time.time()) == pytest.approx(86400, abs=5)
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd backend && pytest open_webui/test/routers/test_newapi_sso.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'open_webui.routers.newapi_sso'`

- [ ] **Step 3: Write the implementation**

```python
# backend/open_webui/routers/newapi_sso.py
"""
IdP-initiated OAuth2 callback for new-api SSO.

new-api itself starts this hand-off (from a page the user is already
logged into) by sending the browser straight to this URL with a
ready-to-exchange authorization `code` already attached — OpenWebUI never
redirects to new-api to begin the flow. See
docs/superpowers/specs/2026-08-17-newapi-oauth2-sso-integration-design.md.

`state` is received but intentionally not validated: OpenWebUI never issued
it, so there is nothing to check it against. This is an accepted trade-off
of the IdP-initiated flow (see the design spec, "잔여 리스크").
"""

import datetime
import logging
import uuid

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from open_webui.models.auths import Auths
from open_webui.models.config import Config
from open_webui.models.oauth_sessions import OAuthSessions
from open_webui.models.users import Users
from open_webui.utils.auth import get_password_hash
from open_webui.utils.groups import apply_default_group_assignment
from open_webui.utils.newapi_oauth import NewapiOAuthError, exchange_code_for_token, fetch_userinfo

log = logging.getLogger(__name__)

router = APIRouter()

# Forced regardless of the deployment's global auth.jwt_expiry — see the
# design spec's "세션 상한 24h의 근거" for why this bound exists.
NEWAPI_SESSION_TTL = datetime.timedelta(hours=24)


def _reconnect_redirect(reason: str) -> str:
    """
    Redirect to /auth (not /), and carry the failure reason through so the
    login page (Task 10) can show distinct copy for invalid_client (an ops
    problem, retrying won't help) vs everything else (retrying via a fresh
    new-api link will). Redirecting to '/' would get silently swallowed by
    the app's own unauthenticated-route guard before the query string is
    ever read, since no session was created on this failed attempt.
    """
    return f'/auth?error=newapi_sso_failed&reason={reason}'


async def _provision_or_login_user(userinfo: dict, db):
    """Look up by oauth sub, fall back to email, else create. Never trusts caller-supplied identity beyond `userinfo`."""
    user = await Users.get_user_by_oauth_sub('newapi', userinfo['sub'], db=db)
    if not user:
        existing_by_email = await Users.get_user_by_email(userinfo['email'], db=db)
        if existing_by_email:
            await Users.update_user_oauth_by_id(existing_by_email.id, 'newapi', userinfo['sub'], db=db)
            user = await Users.get_user_by_id(existing_by_email.id, db=db)

    if not user:
        user = await Auths.insert_new_auth(
            email=userinfo['email'],
            password=await get_password_hash(str(uuid.uuid4())),  # random, never used to sign in
            name=userinfo['name'],
            role=await Config.get('ui.default_user_role'),
            oauth={'newapi': {'sub': userinfo['sub']}},
            db=db,
        )

        # Race-safe first-user-becomes-admin bootstrap, matching signup_handler's pattern.
        if await Users.get_num_users(db=db) == 1:
            await Users.update_user_role_by_id(user.id, 'admin', db=db)
            user = await Users.get_user_by_id(user.id, db=db)

        await apply_default_group_assignment(await Config.get('ui.default_group_id'), user.id, db=db)

    # Sync admin status from new-api's is_admin claim once it ships (spec
    # §"new-api 팀 의존성"). Only ever promotes — never auto-demotes, so a
    # manually-granted admin (via the retained role-edit UI, Task 9) is
    # never silently revoked just because the claim is absent/False.
    if userinfo.get('is_admin') and user.role != 'admin':
        await Users.update_user_role_by_id(user.id, 'admin', db=db)
        user = await Users.get_user_by_id(user.id, db=db)

    return user


async def _store_newapi_token(user_id: str, access_token: str, expires_in: int, db):
    token = {
        'access_token': access_token,
        'expires_at': int(datetime.datetime.now().timestamp()) + expires_in,
    }
    existing = await OAuthSessions.get_session_by_provider_and_user_id('newapi', user_id, db=db)
    if existing:
        await OAuthSessions.update_session_by_id(existing.id, token, db=db)
    else:
        await OAuthSessions.create_session(user_id, 'newapi', token, db=db)


@router.get('/callback')
async def newapi_callback(request: Request, code: str, state: str = ''):
    from open_webui.internal.db import AsyncSessionLocal
    from open_webui.routers.auths import create_session_response

    async with AsyncSessionLocal() as db:
        try:
            token_response = await exchange_code_for_token(code)
            userinfo = await fetch_userinfo(token_response['access_token'])
        except NewapiOAuthError as e:
            if e.reason == 'invalid_client':
                log.error('new-api SSO misconfigured (invalid_client) — this will not resolve on retry')
            else:
                log.info('new-api SSO callback failed (%s): %s', e.reason, e)
            return RedirectResponse(url=_reconnect_redirect(e.reason), status_code=302)

        user = await _provision_or_login_user(userinfo, db)
        await _store_newapi_token(user.id, token_response['access_token'], token_response['expires_in'], db)

        response = RedirectResponse(url='/', status_code=302)
        await create_session_response(
            request, user, db, response=response, set_cookie=True, source='newapi_sso', expires_delta=NEWAPI_SESSION_TTL
        )
        return response
```

- [ ] **Step 4: Run the test again**

Run: `cd backend && pytest open_webui/test/routers/test_newapi_sso.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/open_webui/routers/newapi_sso.py backend/open_webui/test/routers/test_newapi_sso.py
git commit -m "feat: add /auth/newapi/callback happy path (new user provisioning)"
```

---

### Task 5: Callback — existing user (sub match and email fallback)

**Files:**
- Modify: `backend/open_webui/test/routers/test_newapi_sso.py` (add two tests; no production code changes — `_provision_or_login_user` from Task 4 already implements both paths)

**Interfaces:**
- Consumes/Produces: unchanged from Task 4. This task exists to lock the already-implemented existing-user behavior under test, since Task 4's implementation included it but only the new-user path was verified.

- [ ] **Step 1: Write the failing tests**

Append to `backend/open_webui/test/routers/test_newapi_sso.py`:

```python
@pytest.mark.asyncio
async def test_callback_logs_in_existing_user_by_sub(db_engine, async_client):
    app = FastAPI()
    _mount(app)
    async_client._transport = ASGITransport(app=app)

    async with AsyncSessionLocal() as db:
        existing = await Users.insert_new_user(
            id='user-existing', name='Bob', email='bob@example.com', role='user', db=db
        )
        await Users.update_user_oauth_by_id(existing.id, 'newapi', 'newapi-user-bob', db=db)

    with (
        patch(
            'open_webui.routers.newapi_sso.exchange_code_for_token',
            new=AsyncMock(return_value={'access_token': 'sk-bob', 'expires_in': 86400}),
        ),
        patch(
            'open_webui.routers.newapi_sso.fetch_userinfo',
            new=AsyncMock(
                return_value={'sub': 'newapi-user-bob', 'email': 'bob-changed@example.com', 'name': 'Bob', 'is_admin': False}
            ),
        ),
    ):
        res = await async_client.get('/auth/newapi/callback?code=abc123', follow_redirects=False)

    assert res.status_code == 302

    async with AsyncSessionLocal() as db:
        user = await Users.get_user_by_id('user-existing', db=db)
        # sub match wins even though new-api's email claim changed — no duplicate account created.
        assert user.email == 'bob@example.com'
        assert await Users.get_user_by_email('bob-changed@example.com', db=db) is None


@pytest.mark.asyncio
async def test_callback_matches_existing_local_account_by_email_when_no_sub_link(db_engine, async_client):
    app = FastAPI()
    _mount(app)
    async_client._transport = ASGITransport(app=app)

    async with AsyncSessionLocal() as db:
        await Users.insert_new_user(
            id='user-carol', name='Carol', email='carol@example.com', role='user', db=db
        )

    with (
        patch(
            'open_webui.routers.newapi_sso.exchange_code_for_token',
            new=AsyncMock(return_value={'access_token': 'sk-carol', 'expires_in': 86400}),
        ),
        patch(
            'open_webui.routers.newapi_sso.fetch_userinfo',
            new=AsyncMock(
                return_value={'sub': 'newapi-user-carol', 'email': 'carol@example.com', 'name': 'Carol', 'is_admin': False}
            ),
        ),
    ):
        res = await async_client.get('/auth/newapi/callback?code=abc123', follow_redirects=False)

    assert res.status_code == 302

    async with AsyncSessionLocal() as db:
        user = await Users.get_user_by_id('user-carol', db=db)
        assert user.oauth == {'newapi': {'sub': 'newapi-user-carol'}}
        # Only one account for carol@example.com exists.
        assert await Users.get_num_users(db=db) == 1


@pytest.mark.asyncio
async def test_callback_promotes_existing_user_when_is_admin_claim_present(db_engine, async_client):
    app = FastAPI()
    _mount(app)
    async_client._transport = ASGITransport(app=app)

    async with AsyncSessionLocal() as db:
        existing = await Users.insert_new_user(
            id='user-dave', name='Dave', email='dave@example.com', role='user', db=db
        )
        await Users.update_user_oauth_by_id(existing.id, 'newapi', 'newapi-user-dave', db=db)

    with (
        patch(
            'open_webui.routers.newapi_sso.exchange_code_for_token',
            new=AsyncMock(return_value={'access_token': 'sk-dave', 'expires_in': 86400}),
        ),
        patch(
            'open_webui.routers.newapi_sso.fetch_userinfo',
            new=AsyncMock(
                return_value={'sub': 'newapi-user-dave', 'email': 'dave@example.com', 'name': 'Dave', 'is_admin': True}
            ),
        ),
    ):
        await async_client.get('/auth/newapi/callback?code=abc123', follow_redirects=False)

    async with AsyncSessionLocal() as db:
        user = await Users.get_user_by_id('user-dave', db=db)
        assert user.role == 'admin'
```

- [ ] **Step 2: Run the full test file to verify the new tests pass**

Run: `cd backend && pytest open_webui/test/routers/test_newapi_sso.py -v`
Expected: All 4 tests in the file PASS immediately (the original happy-path test plus these 3 new ones) — Task 4's `_provision_or_login_user` already implements the sub-match, email-fallback, and is_admin-promotion branches. This task's purpose is coverage, not new behavior; if any new test fails, that indicates a bug in Task 4's implementation to fix before proceeding.

- [ ] **Step 3: Commit**

```bash
git add backend/open_webui/test/routers/test_newapi_sso.py
git commit -m "test: cover existing-user sub-match and email-fallback paths for new-api SSO callback"
```

---

### Task 6: Callback — error paths

**Files:**
- Modify: `backend/open_webui/test/routers/test_newapi_sso.py` (add tests)
- No production code changes expected — Task 4's `try/except NewapiOAuthError` already redirects to `_RECONNECT_REDIRECT` for every `NewapiOAuthError` reason. This task locks that behavior under test and verifies log levels are distinguishable.

**Interfaces:** unchanged.

- [ ] **Step 1: Write the failing tests**

Append to `backend/open_webui/test/routers/test_newapi_sso.py`:

```python
import logging

from open_webui.utils.newapi_oauth import NewapiOAuthError


@pytest.mark.parametrize('reason', ['invalid_grant', 'invalid_client', 'network_error', 'invalid_userinfo'])
@pytest.mark.asyncio
async def test_callback_redirects_to_reconnect_on_every_error_reason(db_engine, async_client, reason):
    app = FastAPI()
    _mount(app)
    async_client._transport = ASGITransport(app=app)

    with patch(
        'open_webui.routers.newapi_sso.exchange_code_for_token',
        new=AsyncMock(side_effect=NewapiOAuthError(reason)),
    ):
        res = await async_client.get('/auth/newapi/callback?code=bad-code', follow_redirects=False)

    assert res.status_code == 302
    assert res.headers['location'] == f'/auth?error=newapi_sso_failed&reason={reason}'
    assert 'token' not in res.cookies


@pytest.mark.asyncio
async def test_callback_logs_invalid_client_as_error_not_info(db_engine, async_client, caplog):
    app = FastAPI()
    _mount(app)
    async_client._transport = ASGITransport(app=app)

    with patch(
        'open_webui.routers.newapi_sso.exchange_code_for_token',
        new=AsyncMock(side_effect=NewapiOAuthError('invalid_client')),
    ):
        with caplog.at_level(logging.INFO, logger='open_webui.routers.newapi_sso'):
            await async_client.get('/auth/newapi/callback?code=bad-code', follow_redirects=False)

    error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert any('misconfigured' in r.message for r in error_records)


@pytest.mark.asyncio
async def test_callback_never_creates_a_user_on_userinfo_failure(db_engine, async_client):
    app = FastAPI()
    _mount(app)
    async_client._transport = ASGITransport(app=app)

    with (
        patch(
            'open_webui.routers.newapi_sso.exchange_code_for_token',
            new=AsyncMock(return_value={'access_token': 'sk-x', 'expires_in': 86400}),
        ),
        patch(
            'open_webui.routers.newapi_sso.fetch_userinfo',
            new=AsyncMock(side_effect=NewapiOAuthError('invalid_userinfo')),
        ),
    ):
        await async_client.get('/auth/newapi/callback?code=abc', follow_redirects=False)

    async with AsyncSessionLocal() as db:
        assert await Users.get_num_users(db=db) == 0
```

- [ ] **Step 2: Run the tests**

Run: `cd backend && pytest open_webui/test/routers/test_newapi_sso.py -v`
Expected: All PASS. If `test_callback_logs_invalid_client_as_error_not_info` fails, check that the `log.error(...)` call in Task 4's `newapi_callback` uses the module-level `log` (not `print`) and that no earlier code path swallows the exception before it reaches that branch.

- [ ] **Step 3: Commit**

```bash
git add backend/open_webui/test/routers/test_newapi_sso.py
git commit -m "test: cover new-api SSO callback error paths (invalid_grant/invalid_client/network/userinfo)"
```

---

### Task 7: Register the router and expose config to the frontend

**Files:**
- Modify: `backend/open_webui/main.py:139-171` (router import block)
- Modify: `backend/open_webui/main.py:785-829` (router registration block)
- Modify: `backend/open_webui/main.py:2069-2164` (`get_app_config`)
- Test: `backend/open_webui/test/test_main_config_endpoint.py`

**Interfaces:**
- Produces: `GET /api/config` response gains a `newapi_sso` key: `{'enable': bool, 'entry_url': str}`.

- [ ] **Step 1: Write the failing test**

```python
# backend/open_webui/test/test_main_config_endpoint.py
import pytest
from httpx import ASGITransport


@pytest.mark.asyncio
async def test_app_config_exposes_newapi_sso_settings(db_engine, async_client, monkeypatch):
    monkeypatch.setattr('open_webui.main.ENABLE_NEWAPI_SSO', True)
    monkeypatch.setattr('open_webui.main.NEWAPI_ENTRY_URL', 'https://newapi.example.com/console/token')

    from open_webui.main import app

    async_client._transport = ASGITransport(app=app)
    res = await async_client.get('/api/config')

    assert res.status_code == 200
    assert res.json()['newapi_sso'] == {'enable': True, 'entry_url': 'https://newapi.example.com/console/token'}
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd backend && pytest open_webui/test/test_main_config_endpoint.py -v`
Expected: FAIL — `KeyError: 'newapi_sso'`

- [ ] **Step 3: Wire up the router import**

In `backend/open_webui/main.py`, inside the existing `from open_webui.routers import (...)` block (line 139), add `newapi_sso` in alphabetical order between `notifications` and `notes` — actually alphabetically `newapi_sso` sorts before `notes`, so insert it between `notifications`... check exact current ordering; the block is alphabetical, so insert right after `models` and before `notifications`:

```python
from open_webui.routers import (
    analytics,
    audio,
    auths,
    automations,
    calendar,
    channels,
    chats,
    configs,
    evaluations,
    files,
    folders,
    functions,
    groups,
    images,
    knowledge,
    memories,
    models,
    newapi_sso,
    notifications,
    notes,
    ollama,
    openai,
    pipelines,
    prompts,
    retrieval,
    scim,
    skills,
    tasks,
    terminals,
    tools,
    users,
    utils,
)
```

Also add, near the other `open_webui.env` imports in `main.py`:

```python
from open_webui.env import (
    ENABLE_NEWAPI_SSO,
    NEWAPI_ENTRY_URL,
)
```

(If `main.py` already has a multi-line `from open_webui.env import (...)` block, add these two names into it in alphabetical order instead of creating a second import statement.)

- [ ] **Step 4: Register the router**

In `backend/open_webui/main.py`, immediately after the line `app.include_router(auths.router, prefix='/api/v1/auths', tags=['auths'])` (line 798):

```python
app.include_router(newapi_sso.router, prefix='/auth/newapi', tags=['newapi_sso'])
```

- [ ] **Step 5: Add the config fields**

In `backend/open_webui/main.py`, inside `get_app_config` (starting at line 2069), add to the returned dict, alongside the existing `'oauth': {...}` key:

```python
        'newapi_sso': {
            'enable': ENABLE_NEWAPI_SSO,
            'entry_url': NEWAPI_ENTRY_URL,
        },
```

- [ ] **Step 6: Run the test again**

Run: `cd backend && pytest open_webui/test/test_main_config_endpoint.py -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add backend/open_webui/main.py backend/open_webui/test/test_main_config_endpoint.py
git commit -m "feat: register /auth/newapi router and expose newapi_sso config to the frontend"
```

---

### Task 8: Per-user token for the OpenAI connection (`newapi_session` auth type)

**Files:**
- Modify: `backend/open_webui/routers/openai.py:156-219` (`get_headers_and_cookies`)
- Test: `backend/open_webui/test/routers/test_openai_headers.py`

**Interfaces:**
- Consumes: `open_webui.models.oauth_sessions.OAuthSessions.get_session_by_provider_and_user_id`.
- Produces: `get_headers_and_cookies(..., config={'auth_type': 'newapi_session', ...}, user=<UserModel>)` returns headers with `Authorization: Bearer <that user's stored access_token>`, or raises `HTTPException(424, detail='NEWAPI_RECONNECT_REQUIRED')` when no valid token is stored for that user. **424**, not 401, is used deliberately so this never collides with OpenWebUI's own session-invalid handling (design spec did not specify a status code; this plan fixes it at 424 "Failed Dependency" to keep the two failure modes distinguishable end-to-end, matching Task 11's frontend check).

- [ ] **Step 1: Write the failing tests**

```python
# backend/open_webui/test/routers/test_openai_headers.py
import time

import pytest
from fastapi import HTTPException, Request
from open_webui.internal.db import AsyncSessionLocal
from open_webui.models.oauth_sessions import OAuthSessions
from open_webui.models.users import UserModel


def _fake_request():
    scope = {'type': 'http', 'method': 'GET', 'path': '/', 'headers': [], 'query_string': b''}
    return Request(scope)


def _fake_user(id='user-1'):
    return UserModel(
        id=id, name='Test', email='test@example.com', role='user',
        last_active_at=0, updated_at=0, created_at=0,
    )


@pytest.mark.asyncio
async def test_newapi_session_auth_type_uses_stored_token(db_engine):
    from open_webui.routers.openai import get_headers_and_cookies

    user = _fake_user()
    async with AsyncSessionLocal() as db:
        await OAuthSessions.create_session(
            user.id, 'newapi', {'access_token': 'sk-stored', 'expires_at': int(time.time()) + 3600}, db=db
        )

    headers, _ = await get_headers_and_cookies(
        _fake_request(), 'https://newapi.example.com/v1', config={'auth_type': 'newapi_session'}, user=user
    )

    assert headers['Authorization'] == 'Bearer sk-stored'


@pytest.mark.asyncio
async def test_newapi_session_auth_type_raises_when_no_session_stored(db_engine):
    from open_webui.routers.openai import get_headers_and_cookies

    with pytest.raises(HTTPException) as exc_info:
        await get_headers_and_cookies(
            _fake_request(), 'https://newapi.example.com/v1', config={'auth_type': 'newapi_session'}, user=_fake_user()
        )

    assert exc_info.value.status_code == 424
    assert exc_info.value.detail == 'NEWAPI_RECONNECT_REQUIRED'


@pytest.mark.asyncio
async def test_newapi_session_auth_type_raises_when_session_expired(db_engine):
    from open_webui.routers.openai import get_headers_and_cookies

    user = _fake_user()
    async with AsyncSessionLocal() as db:
        await OAuthSessions.create_session(
            user.id, 'newapi', {'access_token': 'sk-stale', 'expires_at': int(time.time()) - 10}, db=db
        )

    with pytest.raises(HTTPException) as exc_info:
        await get_headers_and_cookies(
            _fake_request(), 'https://newapi.example.com/v1', config={'auth_type': 'newapi_session'}, user=user
        )

    assert exc_info.value.status_code == 424


@pytest.mark.asyncio
async def test_newapi_session_auth_type_does_not_fall_back_to_key(db_engine):
    from open_webui.routers.openai import get_headers_and_cookies

    with pytest.raises(HTTPException):
        await get_headers_and_cookies(
            _fake_request(),
            'https://newapi.example.com/v1',
            key='admin-key-should-not-be-used',
            config={'auth_type': 'newapi_session'},
            user=_fake_user(),
        )
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd backend && pytest open_webui/test/routers/test_openai_headers.py -v`
Expected: FAIL — the current implementation falls through the `if/elif` chain with no matching branch, `token` stays `None`, and no `Authorization` header is set at all — none of the three assertions raising `HTTPException` will pass.

- [ ] **Step 3: Modify `get_headers_and_cookies`**

In `backend/open_webui/routers/openai.py`, replace the `auth_type` dispatch block (currently lines 182-213):

```python
    token = None
    auth_type = config.get('auth_type')

    if auth_type == 'bearer' or auth_type is None:
        # Default to bearer if not specified
        token = f'{key}'
    elif auth_type == 'none':
        token = None
    elif auth_type == 'session':
        cookies = request.cookies
        token = request.state.token.credentials
    elif auth_type == 'system_oauth':
        cookies = request.cookies

        oauth_token = None
        try:
            if request.cookies.get('oauth_session_id', None):
                oauth_token = await request.app.state.oauth_manager.get_oauth_token(
                    user.id,
                    request.cookies.get('oauth_session_id', None),
                )
        except Exception as e:
            log.error(f'Error getting OAuth token: {e}')

        if oauth_token:
            token = f'{oauth_token.get("access_token", "")}'

    elif auth_type == 'newapi_session':
        # Per-user credential so new-api attributes usage/billing to the
        # actual calling user rather than a shared admin key. See
        # docs/superpowers/specs/2026-08-17-newapi-oauth2-sso-integration-design.md.
        from open_webui.models.oauth_sessions import OAuthSessions

        session = await OAuthSessions.get_session_by_provider_and_user_id('newapi', user.id) if user else None
        if not session or session.expires_at <= int(time.time()):
            raise HTTPException(status_code=424, detail='NEWAPI_RECONNECT_REQUIRED')
        token = session.token.get('access_token')

    elif auth_type in ('azure_ad', 'microsoft_entra_id'):
        token = get_microsoft_entra_id_access_token()

    if token:
        headers['Authorization'] = f'Bearer {token}'

    if config.get('headers') and isinstance(config.get('headers'), dict):
        custom_headers = await get_custom_headers(config.get('headers'), user, metadata, request=request)
        headers.update(custom_headers)

    return headers, cookies
```

Add `import time` to the top of `backend/open_webui/routers/openai.py` (it currently has no top-level `time` import — verify with `grep -n '^import time' backend/open_webui/routers/openai.py` before adding; if absent, add it alphabetically among the other stdlib imports at the top of the file, next to `import re`).

- [ ] **Step 4: Run the tests again**

Run: `cd backend && pytest open_webui/test/routers/test_openai_headers.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add backend/open_webui/routers/openai.py backend/open_webui/test/routers/test_openai_headers.py
git commit -m "feat: add newapi_session auth_type for per-user OpenAI connection credentials"
```

---

### Task 9: `ui.enable_user_management` config flag + gate admin Add/Delete User UI

**Files:**
- Modify: `backend/open_webui/config.py` (add to `DEFAULT_CONFIG`)
- Modify: `backend/open_webui/main.py` (add to the `Config.get_many(...)` call inside `get_app_config`, and to the returned `features` dict)
- Modify: `src/lib/components/admin/Users/UserList.svelte:212-219` and `:444-457`
- Test: `backend/open_webui/test/test_main_config_endpoint.py` (extend)

**Interfaces:**
- Produces: `GET /api/config` → `features.enable_user_management: bool` (default `True`, so existing deployments are unaffected until explicitly turned off).

- [ ] **Step 1: Write the failing test**

Append to `backend/open_webui/test/test_main_config_endpoint.py`:

```python
@pytest.mark.asyncio
async def test_app_config_defaults_enable_user_management_to_true():
    from open_webui.main import app

    async_client_transport_app = app
    from httpx import ASGITransport, AsyncClient

    async with AsyncClient(transport=ASGITransport(app=async_client_transport_app), base_url='http://test') as client:
        res = await client.get('/api/config')

    assert res.json()['features']['enable_user_management'] is True
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd backend && pytest open_webui/test/test_main_config_endpoint.py -v`
Expected: FAIL — `KeyError: 'enable_user_management'`

- [ ] **Step 3: Add the config default**

In `backend/open_webui/config.py`, inside the `DEFAULT_CONFIG` dict (the large dict assembled around lines 2788-3183), add:

```python
    'ui.enable_user_management': True,
```

(Place it alongside the other `ui.enable_*` keys already in that dict, e.g. next to `'ui.enable_signup'`, for readability — exact position within the dict has no functional effect.)

- [ ] **Step 4: Expose it through `/api/config`**

In `backend/open_webui/main.py`, add `'ui.enable_user_management'` to the `Config.get_many(...)` call inside `get_app_config` (alongside `'ui.enable_signup'`), and add to the returned `features` dict:

```python
            'enable_user_management': config.get('ui.enable_user_management', True),
```

- [ ] **Step 5: Run the test again**

Run: `cd backend && pytest open_webui/test/test_main_config_endpoint.py -v`
Expected: PASS

- [ ] **Step 6: Gate the frontend buttons**

In `src/lib/components/admin/Users/UserList.svelte`, wrap the existing "Add User" button (currently lines 212-219) in a conditional:

```svelte
			{#if $config?.features?.enable_user_management ?? true}
				<button
					class="ml-1 shrink-0 rounded-lg bg-gray-50 px-2.5 py-1 text-xs text-gray-900 transition ring-1 ring-gray-200 hover:bg-gray-100 dark:bg-gray-850 dark:text-gray-100 dark:ring-gray-800 dark:hover:bg-gray-800"
					on:click={() => {
						showAddUserModal = !showAddUserModal;
					}}
				>
					{$i18n.t('Add User')}
				</button>
			{/if}
```

And wrap the existing per-row delete button (currently lines 444-457) so it now requires *both* conditions:

```svelte
								{#if user.role !== 'admin' && ($config?.features?.enable_user_management ?? true)}
									<Tooltip content={$i18n.t('Delete User')}>
										<button
											class="self-center w-fit p-1.5 hover:bg-black/5 dark:hover:bg-white/5 rounded-lg"
											aria-label={$i18n.t('Delete User')}
											on:click={async () => {
												showDeleteConfirmDialog = true;
												selectedUser = user;
											}}
										>
											<Trash className="size-3.5" />
										</button>
									</Tooltip>
								{/if}
```

`config` must already be imported from `$lib/stores` in this file's `<script>` block — verify with `grep -n "from '\$lib/stores'" src/lib/components/admin/Users/UserList.svelte`; if `config` is not already in that import's destructured list, add it.

The role-edit button (`EditPencil`, lines ~430-441) is left completely untouched — role changes stay available regardless of this flag, per the design spec's "역할 변경 기능만 임시로 유지" decision.

- [ ] **Step 7: Commit**

```bash
git add backend/open_webui/config.py backend/open_webui/main.py backend/open_webui/test/test_main_config_endpoint.py src/lib/components/admin/Users/UserList.svelte
git commit -m "feat: add ui.enable_user_management flag, gate Add/Delete User UI"
```

---

### Task 10: Login page — new-api entry point

**Files:**
- Modify: `src/routes/auth/+page.svelte`

No backend test applies here (pure UI); this task has no automated test per the plan's scope — verify manually per the Manual Verification section at the end of this plan.

- [ ] **Step 1: Read the failure reason, if any**

In `src/routes/auth/+page.svelte`, inside the existing `onMount` block, near where `form` is already read from the URL (around line 172, `form = $page.url.searchParams.get('form');`), add:

```ts
	let newapiSsoErrorReason = '';
```

as a top-level `<script>` variable (alongside the existing `let form` declaration), and in `onMount`, alongside the existing `form = $page.url.searchParams.get('form');` line:

```ts
		if ($page.url.searchParams.get('error') === 'newapi_sso_failed') {
			newapiSsoErrorReason = $page.url.searchParams.get('reason') ?? '';
		}
```

- [ ] **Step 2: Add the entry button and error message**

Locate the block that currently reads (around line 288 and mirrored around lines 381/446):

```svelte
								{#if $config?.features.enable_login_form || $config?.features.enable_ldap || form}
```

Add a new, independent block right after the *first* occurrence of that conditional's closing `{/if}` (i.e., as a sibling, not nested inside it), so it renders regardless of whether the local form is shown:

```svelte
							{#if $config?.newapi_sso?.enable}
								{#if newapiSsoErrorReason === 'invalid_client'}
									<div class="mt-4 text-center text-sm text-red-600 dark:text-red-400">
										{$i18n.t('Sign-in is temporarily unavailable. Please try again shortly.')}
									</div>
								{:else if newapiSsoErrorReason}
									<div class="mt-4 text-center text-sm text-gray-500 dark:text-gray-400">
										{$i18n.t('That sign-in link expired or was already used.')}
									</div>
								{/if}
								<div class="mt-2 text-center">
									<a
										href={$config?.newapi_sso?.entry_url}
										class="text-sm font-medium text-gray-700 dark:text-gray-200 underline"
									>
										{$i18n.t('Sign in with new-api')}
									</a>
								</div>
							{/if}
```

`invalid_client` gets distinct copy because it is a deployment misconfiguration — clicking the link again will not fix it (design spec, error-handling table) — while every other reason (`invalid_grant`, `network_error`, `invalid_userinfo`) is safely retryable by getting a fresh link from new-api.

- [ ] **Step 3: Manually verify**

Run the frontend dev server (`npm run dev`), set `ENABLE_NEWAPI_SSO=true` and `NEWAPI_ENTRY_URL=https://example.com/console/token` in the backend env, restart the backend, and confirm:
1. Visiting `/auth` shows a "Sign in with new-api" link pointing at `NEWAPI_ENTRY_URL`.
2. With `ui.enable_login_form=false` also set, the local form disappears and the new-api link is still visible (this is the actual target deployment configuration).
3. Visiting `/auth?error=newapi_sso_failed&reason=invalid_client` shows the "temporarily unavailable" message; `/auth?error=newapi_sso_failed&reason=invalid_grant` shows the "expired or already used" message.

- [ ] **Step 3: Commit**

```bash
git add src/routes/auth/+page.svelte
git commit -m "feat: show new-api sign-in entry point on the login page"
```

---

### Task 11: Reconnect banner on `NEWAPI_RECONNECT_REQUIRED`

**Files:**
- Modify: `src/lib/components/chat/Messages/ResponseMessage.svelte:878-879`
- Create: `src/lib/components/chat/Messages/NewapiReconnectBanner.svelte`

No backend changes — Task 8 already causes `main.py`'s existing generic chat-error handler (`error_detail = e.detail if isinstance(e, HTTPException) else str(e)`, `backend/open_webui/main.py:1584-1607`) to emit `{'type': 'chat:message:error', 'data': {'error': {'content': 'NEWAPI_RECONNECT_REQUIRED'}}}` unmodified, which `Chat.svelte:1018-1019` already assigns to `message.error`. This task only changes how that specific marker renders.

- [ ] **Step 1: Create the banner component**

```svelte
<!-- src/lib/components/chat/Messages/NewapiReconnectBanner.svelte -->
<script lang="ts">
	import { getContext } from 'svelte';
	import { config } from '$lib/stores';

	const i18n = getContext('i18n');
</script>

<div
	class="flex items-center gap-2 text-sm text-yellow-700 dark:text-yellow-300 bg-yellow-50 dark:bg-yellow-900/20 rounded-lg px-3 py-2"
>
	<span>{$i18n.t('Your connection to new-api has expired.')}</span>
	<a href={$config?.newapi_sso?.entry_url} class="underline font-medium">
		{$i18n.t('Click here to reconnect')}
	</a>
</div>
```

- [ ] **Step 2: Special-case the marker in `ResponseMessage.svelte`**

In `src/lib/components/chat/Messages/ResponseMessage.svelte`, replace line 879:

```svelte
								<Error content={message?.error?.content ?? message.content} />
```

with:

```svelte
								{#if message?.error?.content === 'NEWAPI_RECONNECT_REQUIRED'}
									<NewapiReconnectBanner />
								{:else}
									<Error content={message?.error?.content ?? message.content} />
								{/if}
```

Add the import near the top of the file's `<script>` block, alongside the existing `Error` component import (find it with `grep -n "import Error from" src/lib/components/chat/Messages/ResponseMessage.svelte`):

```ts
	import NewapiReconnectBanner from './NewapiReconnectBanner.svelte';
```

- [ ] **Step 3: Manually verify**

With a `newapi_session`-mode connection configured and no `OAuthSessions` row for the current user (or an expired one — see Task 8), send a chat message and confirm the yellow reconnect banner appears instead of the generic red error, and that its link points at `NEWAPI_ENTRY_URL`.

- [ ] **Step 4: Commit**

```bash
git add src/lib/components/chat/Messages/NewapiReconnectBanner.svelte src/lib/components/chat/Messages/ResponseMessage.svelte
git commit -m "feat: show a reconnect banner instead of a generic error on NEWAPI_RECONNECT_REQUIRED"
```

---

## Manual Verification (cannot be automated within this repo)

Per the design spec's "자동화 불가 영역": before go-live, confirm against a real new-api instance (not just this plan's mocks) that:
1. The registered `redirect_uri` on new-api's side exactly matches this deployment's `/auth/newapi/callback` URL.
2. A real authorization code, once used, is rejected on a second attempt with `invalid_grant`.
3. Logging out of new-api causes the *next* chat request in an already-open OpenWebUI tab to show the reconnect banner (validates the 24h/immediate-invalidation contract end-to-end, not just OpenWebUI's side of it).
4. The "Open in Chat App" link on new-api's side is not renderable as a plain copyable URL (per the link-handling requirement sent to the new-api team).

## Deployment Checklist

Set together when enabling this feature:

```
ENABLE_NEWAPI_SSO=true
NEWAPI_OAUTH_BASE_URL=https://<newapi-host>
NEWAPI_OAUTH_CLIENT_ID=<from new-api ops>
NEWAPI_OAUTH_CLIENT_SECRET=<from new-api ops>
NEWAPI_ENTRY_URL=<URL back to new-api's login/entry screen>
ENABLE_PASSWORD_AUTH=false
```

And via the admin config API / `Config.upsert`:
```
ui.enable_login_form = false
ui.enable_signup = false
auth.enable_api_keys = false
ui.enable_user_management = false
auth.jwt_expiry = 24h
```

(`ui.enable_password_change_form` needs no separate change — `Account.svelte` already only shows that tab when `enable_login_form` is also true, so it's already hidden by the first flag above.)
