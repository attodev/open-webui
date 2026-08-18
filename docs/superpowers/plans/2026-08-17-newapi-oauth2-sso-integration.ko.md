# new-api OAuth2 SSO 연동 구현 계획

> **에이전트 작업자용:** 필수 서브스킬: 이 계획을 태스크 단위로 실행하려면 superpowers:subagent-driven-development(권장) 또는 superpowers:executing-plans을 사용할 것. 각 단계는 체크박스(`- [ ]`) 문법으로 진행 상황을 추적한다.

**목표:** OpenWebUI가 오직 new-api의 IdP-initiated OAuth2 핸드오프를 통해서만 인증하도록 만들고, 사용자별 LLM 과금을 동일한 토큰으로 라우팅한다. `docs/superpowers/specs/2026-08-17-newapi-oauth2-sso-integration-design.md` 기준.

**아키텍처:** 새로 만드는 독립 라우터(`routers/newapi_sso.py`)가 new-api가 이미 발급한 인증 코드를 받아 서버 대 서버로 액세스 토큰과 교환하고, 기존 세션 생성 헬퍼를 통해 로컬 사용자를 프로비저닝/로그인시키며, 그 토큰을 기존 `OAuthSessions` 테이블에 저장한다. OpenAI 커넥션 계층(`routers/openai.py`)에 새 `auth_type` 분기를 추가해, 관리자 고정 키 대신 요청마다 저장된 토큰을 사용자별로 조회한다. 기능 제거는 거의 전부 기존 설정 플래그(`ENABLE_PASSWORD_AUTH`, `ui.enable_login_form`, `ui.enable_signup`, `auth.enable_api_keys`, `ui.enable_password_change_form`)를 `false`로 설정하는 것만으로 달성되며, 사용자 관리(추가/삭제)와 로그인 페이지의 new-api 진입점만 새 UI 코드가 필요하다.

**기술 스택:** FastAPI, SQLAlchemy(비동기), aiohttp(아웃바운드 HTTP), pytest + pytest-asyncio + httpx `ASGITransport`(신규 백엔드 테스트 하네스 — 이 저장소에는 현재 하나도 없음), Svelte 5 프론트엔드.

## 전역 제약 조건 (Global Constraints)

- new-api 쪽에는 리프레시 토큰이 존재하지 않는다 — 만료/로그아웃 시에는 오직 "new-api에서 새 코드를 받는" 방법뿐이며, 조용한 리프레시는 없다 (spec §3, §"new-api 팀 의존성").
- 인증코드(`code`)는 1회용이며 120초 TTL을 갖는다. 실패한 교환을 같은 코드로 재시도하지 않는다 (spec "에러 처리 및 엣지 케이스").
- 이 흐름에서 발급하는 OpenWebUI 세션은 전역 `auth.jwt_expiry` 값과 무관하게 반드시 24시간 고정 만료여야 하며, 활동에 따라 슬라이딩 연장되면 안 된다 (spec "Section A/B 정정" 및 "세션 상한 24h의 근거").
- 콜백은 들어오는 요청에 담긴 기존 쿠키를 절대 신뢰해서는 안 된다 — 신원은 오직 새로 교환한 토큰에서만 나온다 (spec "컴포넌트 상세 > 1. 신규 라우트").
- 이메일에 대한 도메인 화이트리스트는 두지 않는다 (spec "이메일 도메인 제한" 결정: 무제한 허용).
- `ENABLE_NEWAPI_SSO`가 설정된 이후에는 로컬 비밀번호 로그인에 대한 비상 우회 경로를 두지 않는다 — `ENABLE_PASSWORD_AUTH=False`는 런타임 오버라이드 없는 배포 시점 결정이다 (spec "결정" 콜아웃).
- 새로 추가하는 모든 환경변수는 런타임 `Config` 테이블이 아니라 이 저장소의 `backend/open_webui/env.py`에 있는 `os.getenv(...)` 관례를 따른다 (spec "설정값").

---

## 파일 구조

| 파일 | 변경 내용 |
|---|---|
| `backend/open_webui/test/__init__.py` | 신규, 빈 파일 |
| `backend/open_webui/test/conftest.py` | 신규 — pytest 픽스처: 임시 SQLite DB, ASGI 테스트 클라이언트 |
| `backend/open_webui/routers/auths.py` | `create_session_response`가 `expires_delta` 오버라이드를 받도록 수정 |
| `backend/open_webui/utils/newapi_oauth.py` | 신규 — `exchange_code_for_token`, `fetch_userinfo`, 환경변수 기반 설정 |
| `backend/open_webui/env.py` | `ENABLE_NEWAPI_SSO`, `NEWAPI_OAUTH_BASE_URL`, `NEWAPI_OAUTH_CLIENT_ID`, `NEWAPI_OAUTH_CLIENT_SECRET`, `NEWAPI_ENTRY_URL` 추가 |
| `backend/open_webui/routers/newapi_sso.py` | 신규 — `GET /auth/newapi/callback` |
| `backend/open_webui/main.py` | `newapi_sso.router` 등록; `get_app_config()`에 `newapi_sso`와 `enable_user_management` 필드 추가 |
| `backend/open_webui/routers/openai.py` | `get_headers_and_cookies`: 새 `auth_type == 'newapi_session'` 분기 |
| `backend/open_webui/config.py` | `DEFAULT_CONFIG`에 `'ui.enable_user_management': True` 추가 |
| `src/lib/components/admin/Users/UserList.svelte` | Add User 버튼 + 행별 Delete 버튼을 `$config.features.enable_user_management` 뒤로 게이팅 |
| `src/routes/auth/+page.svelte` | SSO가 활성화되면 new-api 진입 버튼을 렌더링 |
| `src/lib/components/chat/Messages/ResponseMessage.svelte` | `NEWAPI_RECONNECT_REQUIRED` 에러 마커를 특수 처리 |

아래 태스크 중 백엔드를 건드리는 모든 태스크는 **Task 1**(테스트 하네스)이 먼저 완료되어 있어야 한다.

---

### Task 1: 백엔드 테스트 하네스

이 저장소에는 현재 `test_*.py` 파일이 하나도 없으므로, 이 태스크는 나머지 계획에 필요한 최소한의 pytest 설정을 만든다: 테스트 함수마다 격리된 임시 파일 SQLite 데이터베이스, 그리고 `ASGITransport`를 통해 FastAPI 앱에 직접 연결되는 `httpx.AsyncClient`(실행 중인 서버도, 포트도 필요 없음).

**파일:**
- 생성: `backend/open_webui/test/__init__.py`
- 생성: `backend/open_webui/test/conftest.py`
- 테스트: `backend/open_webui/test/test_harness_smoke.py`

**인터페이스:**
- 산출물: `db_engine` 픽스처(동기 SQLAlchemy `Engine`, `Base.metadata.create_all`로 스키마 생성), `async_client` 픽스처(`httpx.AsyncClient`, base_url `http://test`) — 둘 다 이후의 모든 백엔드 태스크 테스트에서 사용된다.

- [ ] **Step 1: 실패하는 스모크 테스트 작성**

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

- [ ] **Step 2: 실행해서 실패하는지 확인**

실행: `cd backend && pytest open_webui/test/test_harness_smoke.py -v`
예상: FAIL — `fixture 'async_client' not found` (아직 conftest.py가 없음).

- [ ] **Step 3: `backend/open_webui/test/__init__.py` 생성**

```python
```

(빈 파일 — `open_webui.test`를 import 가능한 패키지로 만들어, 나중에 테스트 모듈이 필요하면 상대 import를 쓸 수 있게 한다.)

- [ ] **Step 4: `conftest.py` 작성**

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

> 참고(최종 수정 반영): 실제 구현에서는 `DATABASE_URL`/`ENABLE_DB_MIGRATIONS`를 `setdefault`가 아니라 **무조건 덮어쓰기**로 바꿨다 — 개발자 셸에 이미 실제 DB를 가리키는 `DATABASE_URL`이 설정돼 있으면, `setdefault`는 그 값을 그대로 써버려서 `db_engine` 픽스처의 `drop_all`이 실제 DB의 테이블을 지워버릴 위험이 있었기 때문이다(리뷰에서 발견된 실제 결함). `WEBUI_SECRET_KEY`/`WEBUI_AUTH`는 그대로 `setdefault`로 남겨도 안전하다.

- [ ] **Step 5: 스모크 테스트 재실행**

실행: `cd backend && pytest open_webui/test/test_harness_smoke.py -v`
예상: PASS

- [ ] **Step 6: 커밋**

```bash
git add backend/open_webui/test/__init__.py backend/open_webui/test/conftest.py backend/open_webui/test/test_harness_smoke.py
git commit -m "test: add backend pytest harness (temp SQLite DB + ASGI test client)"
```

---

### Task 2: `create_session_response`가 `expires_delta` 오버라이드를 받도록

**파일:**
- 수정: `backend/open_webui/routers/auths.py:164-215` (`create_session_response` 함수)
- 테스트: `backend/open_webui/test/routers/test_auths_session_response.py`

**인터페이스:**
- 소비: `open_webui.internal.db.get_async_session`(원시 `AsyncSessionLocal` 경유), `open_webui.models.users.Users.insert_new_user`.
- 산출물: `create_session_response(request, user, db, response=None, set_cookie=False, source='api', expires_delta: datetime.timedelta | None = None) -> dict`. `expires_delta`가 전달되면 `Config.get('auth.jwt_expiry')`보다 우선한다. Task 5는 이 정확한 파라미터명과 동작에 의존한다.

- [ ] **Step 1: 실패하는 테스트 작성**

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

- [ ] **Step 2: 실행해서 실패하는지 확인**

실행: `cd backend && pytest open_webui/test/routers/test_auths_session_response.py -v`
예상: FAIL — `TypeError: create_session_response() got an unexpected keyword argument 'expires_delta'`

- [ ] **Step 3: `create_session_response` 수정**

`backend/open_webui/routers/auths.py`에서 기존 함수(현재 164-215줄)를 다음으로 교체:

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

(시그니처와 본문 첫 네 줄만 바뀌었다 — 나머지는 현재 구현과 동일.)

- [ ] **Step 4: 테스트 재실행**

실행: `cd backend && pytest open_webui/test/routers/test_auths_session_response.py -v`
예상: PASS

- [ ] **Step 5: 커밋**

```bash
git add backend/open_webui/routers/auths.py backend/open_webui/test/routers/__init__.py backend/open_webui/test/routers/test_auths_session_response.py
git commit -m "feat: allow create_session_response to override the JWT expiry"
```

---

### Task 3: new-api OAuth2 클라이언트 헬퍼 (`utils/newapi_oauth.py`)

두 개의 아웃바운드 HTTP 호출(토큰 교환, userinfo)을 분리해서 실제 new-api 서버 없이 단위 테스트할 수 있게 하고, 라우터(Task 4)는 오케스트레이션에만 집중하도록 한다.

**파일:**
- 수정: `backend/open_webui/env.py` (새 환경변수 추가, 756번째 줄의 `WEBUI_AUTH_TRUSTED_EMAIL_HEADER` 근처)
- 생성: `backend/open_webui/utils/newapi_oauth.py`
- 테스트: `backend/open_webui/test/utils/test_newapi_oauth.py`

**인터페이스:**
- 산출물:
  - `NewapiOAuthError(Exception)` — `.reason: str` 속성을 갖고 발생하며, `'invalid_grant'`, `'invalid_client'`, `'network_error'`, `'invalid_userinfo'` 중 하나.
  - `async def exchange_code_for_token(code: str) -> dict` — 성공 시 `{'access_token': str, 'expires_in': int}`을 반환하고, 실패 시 `NewapiOAuthError`를 발생시킨다.
  - `async def fetch_userinfo(access_token: str) -> dict` — `{'sub': str, 'email': str, 'name': str, 'is_admin': bool}`을 반환한다(`is_admin`은 클레임이 없으면 `False`가 기본값 — design spec 기준 new-api가 아직 이 클레임을 내려주지 않기 때문), 실패하거나 `sub`/`email`이 없으면 `NewapiOAuthError`를 발생시킨다.
- 소비: `open_webui.utils.session_pool.get_session`, `open_webui.utils.session_pool.cleanup_response`.

- [ ] **Step 1: 환경변수 추가**

`backend/open_webui/env.py`에서 `WEBUI_AUTH_TRUSTED_ROLE_HEADER` 줄(758번째 줄) 바로 뒤에:

```python
ENABLE_NEWAPI_SSO = os.getenv('ENABLE_NEWAPI_SSO', 'False').lower() == 'true'
NEWAPI_OAUTH_BASE_URL = os.getenv('NEWAPI_OAUTH_BASE_URL', '').rstrip('/')
NEWAPI_OAUTH_CLIENT_ID = os.getenv('NEWAPI_OAUTH_CLIENT_ID', '')
NEWAPI_OAUTH_CLIENT_SECRET = os.getenv('NEWAPI_OAUTH_CLIENT_SECRET', '')
NEWAPI_ENTRY_URL = os.getenv('NEWAPI_ENTRY_URL', '')
```

- [ ] **Step 2: 실패하는 테스트 작성**

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

- [ ] **Step 3: 실행해서 실패하는지 확인**

실행: `cd backend && pytest open_webui/test/utils/test_newapi_oauth.py -v`
예상: FAIL — `ModuleNotFoundError: No module named 'open_webui.utils.newapi_oauth'`

- [ ] **Step 4: 구현 작성**

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

> 참고(최종 수정 반영): `except aiohttp.ClientError`는 `asyncio.TimeoutError`(총 타임아웃)를 잡지 못하고, `payload['access_token']`처럼 직접 키 접근하는 부분은 키가 없으면 처리되지 않은 `KeyError`를 던진다는 결함이 리뷰에서 발견됐다. 최종 구현은 `except (aiohttp.ClientError, asyncio.TimeoutError, ValueError)`로 넓히고, 필수 키가 없을 때 명시적으로 `NewapiOAuthError`를 발생시키도록 `.get(...)` 기반 접근으로 바꿨다. 아래 예상 테스트 개수도 실제로는 6개다(계획 텍스트의 "(7 tests)" 표기는 오기).

- [ ] **Step 5: 테스트 재실행**

실행: `cd backend && pytest open_webui/test/utils/test_newapi_oauth.py -v`
예상: PASS (6개 테스트)

- [ ] **Step 6: 커밋**

```bash
git add backend/open_webui/env.py backend/open_webui/utils/newapi_oauth.py backend/open_webui/test/utils/__init__.py backend/open_webui/test/utils/test_newapi_oauth.py
git commit -m "feat: add new-api OAuth2 client helper (token exchange + userinfo)"
```

---

### Task 4: `/auth/newapi/callback` — 신규 사용자 happy path

**파일:**
- 생성: `backend/open_webui/routers/newapi_sso.py`
- 테스트: `backend/open_webui/test/routers/test_newapi_sso.py`

**인터페이스:**
- 소비: `exchange_code_for_token`, `fetch_userinfo`(Task 3); `expires_delta`를 받는 `create_session_response`(Task 2); `open_webui.models.users.Users.get_user_by_oauth_sub`, `.get_user_by_email`, `.update_user_oauth_by_id`, `.get_num_users`, `.update_user_role_by_id`, `.get_user_by_id`; `open_webui.models.auths.Auths.insert_new_auth`; `open_webui.models.oauth_sessions.OAuthSessions.get_session_by_provider_and_user_id`, `.create_session`, `.update_session_by_id`; `open_webui.utils.groups.apply_default_group_assignment`.
- 산출물: `GET /callback`을 가진 `router = APIRouter()`(Task 7에서 `/auth/newapi`에 마운트됨), 그리고 `NEWAPI_SESSION_TTL = datetime.timedelta(hours=24)`(Task 8에는 필요 없지만, 전역 제약 조건을 코드 안에 문서화하는 모듈 상수).

- [ ] **Step 1: 실패하는 테스트 작성**

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
    assert res.headers['location'] == '/auth'
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

> 참고(최종 수정 반영): 리다이렉트 목적지는 원래 계획대로 `/`가 아니라 **`/auth`**로 확정됐다 (아래 "Critical(C1) 수정 사항" 참고). 위 테스트의 `location` 값은 최종 수정을 반영한 것이다.

- [ ] **Step 2: 실행해서 실패하는지 확인**

실행: `cd backend && pytest open_webui/test/routers/test_newapi_sso.py -v`
예상: FAIL — `ModuleNotFoundError: No module named 'open_webui.routers.newapi_sso'`

- [ ] **Step 3: 구현 작성**

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

> **Critical(C1) 수정 사항 — 최종 전체 브랜치 리뷰에서 발견:** 위 코드는 최초 구현이며, 실제로는 심각한 버그가 있었다. `create_session_response(..., set_cookie=True)`는 쿠키를 `httponly=True`로 설정하는데, OpenWebUI 프론트엔드는 순수 SPA로서 오직 `localStorage.token`에서만 세션을 부트스트랩한다. 쿠키에서 localStorage로 값을 옮기는 다리 역할인 `oauthCallbackHandler()`(`src/routes/auth/+page.svelte`)는 `document.cookie`를 읽는데, **httpOnly 쿠키는 여기서 보이지 않는다.** 그 결과 로그인에 성공해도 브라우저는 절대 채팅 화면에 도달하지 못하고 `/auth`로 계속 되돌아간다 — 기능 전체가 종단간으로 동작하지 않는 치명적 결함이었다.
>
> 수정: 기존 일반 OAuth 콜백(`utils/oauth.py`)이 이미 쓰던 패턴을 그대로 따라, `create_session_response(..., set_cookie=False)`로 토큰만 받아온 뒤, `/`가 아니라 **`/auth`**로 리다이렉트하고, `httponly=False`로 쿠키를 직접 설정한다:
> ```python
>         user = await _provision_or_login_user(userinfo, db)
>         await _store_newapi_token(user.id, token_response['access_token'], token_response['expires_in'], db)
>
>         session = await create_session_response(
>             request, user, db, set_cookie=False, source='newapi_sso', expires_delta=NEWAPI_SESSION_TTL
>         )
>         response = RedirectResponse(url='/auth', status_code=302)
>         response.set_cookie(
>             key='token',
>             value=session['token'],
>             httponly=False,  # SPA는 document.cookie로 이 값을 읽어야 함
>             samesite=WEBUI_AUTH_COOKIE_SAME_SITE,
>             secure=WEBUI_AUTH_COOKIE_SECURE,
>             max_age=int(NEWAPI_SESSION_TTL.total_seconds()),
>         )
>         return response
> ```
> 이후 여러 차례의 최종 수정 배치를 거치며 이 함수에는 다음도 추가됐다: `ENABLE_NEWAPI_SSO` 플래그 게이팅(꺼져 있으면 토큰 교환 자체를 시도하지 않고 바로 리다이렉트), 네임스페이스가 붙은 요청 빈도 제한(rate limiter), 빈 `code` 파라미터에 대한 방어, 그리고 `NewapiOAuthError`/`IntegrityError` 외의 예상치 못한 예외까지 잡아 항상 우아하게 `/auth`로 리다이렉트시키는 최종 catch-all. 최종 조립된 순서는: 플래그 게이트 → 네임스페이스 rate limit → 빈 code 가드 → (토큰 교환/사용자정보 조회 try, `NewapiOAuthError`/취소/일반 예외 각각 처리) → (사용자 프로비저닝/세션저장/쿠키설정 try, `IntegrityError`/취소/일반 예외 각각 처리)이다.

- [ ] **Step 4: 테스트 재실행**

실행: `cd backend && pytest open_webui/test/routers/test_newapi_sso.py -v`
예상: PASS

- [ ] **Step 5: 커밋**

```bash
git add backend/open_webui/routers/newapi_sso.py backend/open_webui/test/routers/test_newapi_sso.py
git commit -m "feat: add /auth/newapi/callback happy path (new user provisioning)"
```

---

### Task 5: 콜백 — 기존 사용자(sub 매칭과 email fallback)

**파일:**
- 수정: `backend/open_webui/test/routers/test_newapi_sso.py` (테스트 2개 추가; 프로덕션 코드 변경 없음 — Task 4의 `_provision_or_login_user`가 이미 두 경로를 모두 구현하고 있음)

**인터페이스:**
- 소비/산출물: Task 4와 동일, 변경 없음. 이 태스크는 Task 4의 구현이 이미 처리하지만 새 사용자 경로만 검증됐던 "기존 사용자" 동작을 테스트로 고정하기 위해 존재한다.

- [ ] **Step 1: 실패하는 테스트 작성**

`backend/open_webui/test/routers/test_newapi_sso.py`에 추가:

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

- [ ] **Step 2: 전체 테스트 파일을 실행해 새 테스트들이 통과하는지 확인**

실행: `cd backend && pytest open_webui/test/routers/test_newapi_sso.py -v`
예상: 파일 안의 4개 테스트 전부(원래 happy-path 테스트 + 새로 추가한 3개) 즉시 PASS — Task 4의 `_provision_or_login_user`가 sub-매칭, email-fallback, is_admin-승격 분기를 이미 구현하고 있다. 이 태스크의 목적은 새 동작이 아니라 커버리지다; 새 테스트가 실패한다면 그건 Task 4의 구현에 있는 버그를 진행 전에 고쳐야 한다는 신호다.

- [ ] **Step 3: 커밋**

```bash
git add backend/open_webui/test/routers/test_newapi_sso.py
git commit -m "test: cover existing-user sub-match and email-fallback paths for new-api SSO callback"
```

---

### Task 6: 콜백 — 에러 경로

**파일:**
- 수정: `backend/open_webui/test/routers/test_newapi_sso.py` (테스트 추가)
- 프로덕션 코드 변경은 예상되지 않음 — Task 4의 `try/except NewapiOAuthError`가 이미 모든 `NewapiOAuthError` 사유에 대해 `_RECONNECT_REDIRECT`로 리다이렉트한다. 이 태스크는 그 동작을 테스트로 고정하고 로그 레벨이 구분되는지 검증한다.

**인터페이스:** 변경 없음.

- [ ] **Step 1: 실패하는 테스트 작성**

`backend/open_webui/test/routers/test_newapi_sso.py`에 추가:

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

- [ ] **Step 2: 테스트 실행**

실행: `cd backend && pytest open_webui/test/routers/test_newapi_sso.py -v`
예상: 전부 PASS. `test_callback_logs_invalid_client_as_error_not_info`가 실패한다면, Task 4의 `newapi_callback` 안 `log.error(...)` 호출이 (`print`가 아니라) 모듈 레벨 `log`를 쓰는지, 그리고 그 브랜치에 도달하기 전에 다른 코드 경로가 예외를 삼키지 않는지 확인할 것.

- [ ] **Step 3: 커밋**

```bash
git add backend/open_webui/test/routers/test_newapi_sso.py
git commit -m "test: cover new-api SSO callback error paths (invalid_grant/invalid_client/network/userinfo)"
```

> 참고(최종 수정 반영): 최종 전체 브랜치 리뷰에서 이 함수의 `try/except NewapiOAuthError` 블록이 **토큰 교환/userinfo 호출만** 감싸고 있고, 그 뒤의 사용자 프로비저닝/세션 저장/쿠키 설정 부분은 아무 예외 처리도 없다는 것이 드러났다. 같은 이메일로 동시에 두 번 IdP-initiated 로그인이 들어오면(예: "채팅 열기" 링크를 더블클릭하거나 두 탭에서 여는 경우) 둘 다 신규 사용자 생성 사전 체크를 통과한 뒤 하나가 고유 이메일 제약(`IntegrityError`)에 걸려 처리되지 않은 예외로 새는 문제였다. 이 태스크에서 `IntegrityError`를 잡아 `_reconnect_redirect('server_error')`로 우아하게 리다이렉트하도록 수정했고, 이 정확한 경쟁 상황을 강제로 재현하는 회귀 테스트도 추가했다.

---

### Task 7: 라우터 등록과 프론트엔드로의 설정 노출

**파일:**
- 수정: `backend/open_webui/main.py:139-171` (라우터 import 블록)
- 수정: `backend/open_webui/main.py:785-829` (라우터 등록 블록)
- 수정: `backend/open_webui/main.py:2069-2164` (`get_app_config`)
- 테스트: `backend/open_webui/test/test_main_config_endpoint.py`

**인터페이스:**
- 산출물: `GET /api/config` 응답에 `newapi_sso` 키가 추가됨: `{'enable': bool, 'entry_url': str}`.

- [ ] **Step 1: 실패하는 테스트 작성**

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

- [ ] **Step 2: 실행해서 실패하는지 확인**

실행: `cd backend && pytest open_webui/test/test_main_config_endpoint.py -v`
예상: FAIL — `KeyError: 'newapi_sso'`

- [ ] **Step 3: 라우터 import 연결**

`backend/open_webui/main.py`에서 기존 `from open_webui.routers import (...)` 블록(139번째 줄) 안에, 알파벳 순서로 `models`와 `notifications` 사이에 `newapi_sso`를 추가:

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

`main.py`에 있는 다른 `open_webui.env` import 근처에도 추가:

```python
from open_webui.env import (
    ENABLE_NEWAPI_SSO,
    NEWAPI_ENTRY_URL,
)
```

(만약 `main.py`에 이미 여러 줄짜리 `from open_webui.env import (...)` 블록이 있다면, 별도 import 문을 새로 만들지 말고 그 안에 알파벳 순서로 두 이름을 추가할 것.)

- [ ] **Step 4: 라우터 등록**

`backend/open_webui/main.py`에서 `app.include_router(auths.router, prefix='/api/v1/auths', tags=['auths'])` 줄(798번째 줄) 바로 뒤에:

```python
app.include_router(newapi_sso.router, prefix='/auth/newapi', tags=['newapi_sso'])
```

- [ ] **Step 5: 설정 필드 추가**

`backend/open_webui/main.py`의 `get_app_config`(2069번째 줄부터 시작) 안에서, 반환하는 딕셔너리에 기존 `'oauth': {...}` 키 옆으로 추가:

```python
        'newapi_sso': {
            'enable': ENABLE_NEWAPI_SSO,
            'entry_url': NEWAPI_ENTRY_URL,
        },
```

- [ ] **Step 6: 테스트 재실행**

실행: `cd backend && pytest open_webui/test/test_main_config_endpoint.py -v`
예상: PASS

- [ ] **Step 7: 커밋**

```bash
git add backend/open_webui/main.py backend/open_webui/test/test_main_config_endpoint.py
git commit -m "feat: register /auth/newapi router and expose newapi_sso config to the frontend"
```

---

### Task 8: OpenAI 커넥션의 사용자별 토큰 (`newapi_session` auth type)

**파일:**
- 수정: `backend/open_webui/routers/openai.py:156-219` (`get_headers_and_cookies`)
- 테스트: `backend/open_webui/test/routers/test_openai_headers.py`

**인터페이스:**
- 소비: `open_webui.models.oauth_sessions.OAuthSessions.get_session_by_provider_and_user_id`.
- 산출물: `get_headers_and_cookies(..., config={'auth_type': 'newapi_session', ...}, user=<UserModel>)`는 헤더에 `Authorization: Bearer <해당 사용자의 저장된 access_token>`을 담아 반환하거나, 그 사용자에게 유효한 토큰이 없으면 `HTTPException(424, detail='NEWAPI_RECONNECT_REQUIRED')`를 발생시킨다. **424**를 (401이 아니라) 일부러 쓰는 이유는, OpenWebUI 자체의 "세션 무효" 처리와 절대 충돌하지 않게 하기 위해서다(design spec에는 상태코드가 명시되어 있지 않았음; 이 계획에서 424 "Failed Dependency"로 고정해 두 실패 모드를 종단까지 구분되게 만든다 — Task 11의 프론트엔드 체크와 매칭됨).

- [ ] **Step 1: 실패하는 테스트 작성**

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

- [ ] **Step 2: 실행해서 실패하는지 확인**

실행: `cd backend && pytest open_webui/test/routers/test_openai_headers.py -v`
예상: FAIL — 현재 구현은 `if/elif` 체인 어디에도 걸리지 않고 그대로 통과해버려, `token`이 `None`으로 남고 `Authorization` 헤더가 전혀 설정되지 않는다 — `HTTPException`을 기대하는 세 assertion 모두 실패한다.

- [ ] **Step 3: `get_headers_and_cookies` 수정**

`backend/open_webui/routers/openai.py`에서 `auth_type` 분기 블록(현재 182-213줄)을 다음으로 교체:

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

`backend/open_webui/routers/openai.py` 최상단에 `import time`을 추가한다(현재 최상위 `time` import가 없다 — `grep -n '^import time' backend/open_webui/routers/openai.py`로 미리 확인; 없으면 다른 표준 라이브러리 import들 사이, `import re` 옆에 알파벳 순서로 추가).

> 참고(최종 수정 반영): M2로 발견된 문제 — 세션 행이 존재하고 만료도 안 됐지만 저장된 토큰 딕셔너리에 `access_token` 키가 없는 경우, 위 조건은 통과해버려서 `Authorization` 헤더 없이 요청이 나가게 된다. 최종 구현은 조건을 `if not session or not session.token.get('access_token') or session.expires_at <= int(time.time()):`로 강화했다.
>
> 또한 최종 전체 리뷰(I1)에서, `newapi_session` 커넥션이 로컬 기준으로는 안 만료됐지만 new-api 쪽에서 실제로 토큰을 폐기(로그아웃)한 경우 — 즉 업스트림에서 실제 401이 돌아오는 경우 — 이 분기가 아니라 `generate_chat_completion`(같은 파일의 채팅 완료 엔드포인트)이 그 401을 그대로 통과시켜버려 재연결 배너가 절대 뜨지 않는다는 것이 드러났다. 수정: `generate_chat_completion` 안에서 업스트림 응답이 `>= 400`으로 그대로 전달되는 두 지점(SSE 분기, 일반 JSON 분기) 모두에서, 해당 커넥션의 `auth_type`이 `newapi_session`이고 응답 상태가 401이면 그대로 통과시키는 대신 `HTTPException(424, 'NEWAPI_RECONNECT_REQUIRED')`를 발생시키도록 고쳤다. 이때 반드시 `except HTTPException: raise`를 그 함수의 기존 포괄적인 `except Exception as e: raise HTTPException(status_code=r.status if r else 500, ...)`보다 **앞에** 두어야 한다 — 그렇지 않으면 방금 던진 424가 그 일반 핸들러에 다시 잡혀 401/500으로 뭉개져버린다(실제로 이 순서가 없으면 버그가 재현됨을 실증 테스트로 확인함).

- [ ] **Step 4: 테스트 재실행**

실행: `cd backend && pytest open_webui/test/routers/test_openai_headers.py -v`
예상: PASS (4개 테스트)

- [ ] **Step 5: 커밋**

```bash
git add backend/open_webui/routers/openai.py backend/open_webui/test/routers/test_openai_headers.py
git commit -m "feat: add newapi_session auth_type for per-user OpenAI connection credentials"
```

---

### Task 9: `ui.enable_user_management` 설정 플래그 + 관리자 Add/Delete User UI 게이팅

**파일:**
- 수정: `backend/open_webui/config.py` (`DEFAULT_CONFIG`에 추가)
- 수정: `backend/open_webui/main.py` (`get_app_config` 안의 `Config.get_many(...)` 호출과 반환되는 `features` 딕셔너리에 추가)
- 수정: `src/lib/components/admin/Users/UserList.svelte:212-219`와 `:444-457`
- 테스트: `backend/open_webui/test/test_main_config_endpoint.py` (확장)

**인터페이스:**
- 산출물: `GET /api/config` → `features.enable_user_management: bool` (기본값 `True`, 그래서 명시적으로 끄기 전까지 기존 배포는 영향받지 않음).

- [ ] **Step 1: 실패하는 테스트 작성**

`backend/open_webui/test/test_main_config_endpoint.py`에 추가:

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

- [ ] **Step 2: 실행해서 실패하는지 확인**

실행: `cd backend && pytest open_webui/test/test_main_config_endpoint.py -v`
예상: FAIL — `KeyError: 'enable_user_management'`

- [ ] **Step 3: 설정 기본값 추가**

`backend/open_webui/config.py`의 `DEFAULT_CONFIG` 딕셔너리 안(2788-3183줄 근처에 모여있는 큰 딕셔너리)에 추가:

```python
    'ui.enable_user_management': True,
```

(가독성을 위해 이미 있는 다른 `ui.enable_*` 키들 옆, 예를 들면 `'ui.enable_signup'` 옆에 둘 것 — 딕셔너리 안 정확한 위치는 동작에 영향 없음.)

- [ ] **Step 4: `/api/config`를 통해 노출**

`backend/open_webui/main.py`에서 `get_app_config` 안의 `Config.get_many(...)` 호출에 `'ui.enable_user_management'`를(`'ui.enable_signup'` 옆에) 추가하고, 반환하는 `features` 딕셔너리에도 추가:

```python
            'enable_user_management': config.get('ui.enable_user_management', True),
```

> 참고(최종 수정 반영): 이 필드는 원래 `get_app_config`의 **공개(비인증)** 섹션에 잘못 추가됐었다. 최종 리뷰에서, 이 함수에는 "로그인/회원가입 화면이 인증 전에 필요로 하는" 필드들만 담는 공개 블록과, `if user is not None` 조건으로 감싸인 인증된 사용자 전용 블록(예: `enable_admin_export`, `enable_admin_chat_access`, `enable_admin_analytics`, `enable_folders` 등)이 명확히 구분되어 있음이 확인됐다. `enable_user_management`는 로그인 후에만 렌더링되는 관리자 전용 페이지를 게이팅하므로, 후자의 인증된 블록에 속해야 한다는 게 밝혀져 그쪽으로 옮겼다. 테스트도 관리자로 인증한 뒤 이 필드를 확인하도록, 그리고 비인증 요청에는 이 키 자체가 아예 없음을 확인하는 테스트도 함께 고쳤다.

- [ ] **Step 5: 테스트 재실행**

실행: `cd backend && pytest open_webui/test/test_main_config_endpoint.py -v`
예상: PASS

- [ ] **Step 6: 프론트엔드 버튼 게이팅**

`src/lib/components/admin/Users/UserList.svelte`에서, 기존 "Add User" 버튼(현재 212-219줄)을 조건문으로 감싼다:

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

그리고 기존 행별 delete 버튼(현재 444-457줄)을 감싸 이제 *두* 조건을 모두 요구하도록 한다:

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

이 파일의 `<script>` 블록에서 `config`가 이미 `$lib/stores`에서 import되어 있어야 한다 — `grep -n "from '\$lib/stores'" src/lib/components/admin/Users/UserList.svelte`로 확인할 것; 그 import의 구조분해 목록에 `config`가 없다면 추가할 것.

역할 편집 버튼(`EditPencil`, 약 430-441줄)은 완전히 그대로 둔다 — design spec의 "역할 변경 기능만 임시로 유지" 결정에 따라, 이 플래그와 무관하게 역할 변경은 계속 가능해야 한다.

- [ ] **Step 7: 커밋**

```bash
git add backend/open_webui/config.py backend/open_webui/main.py backend/open_webui/test/test_main_config_endpoint.py src/lib/components/admin/Users/UserList.svelte
git commit -m "feat: add ui.enable_user_management flag, gate Add/Delete User UI"
```

---

### Task 10: 로그인 페이지 — new-api 진입점

**파일:**
- 수정: `src/routes/auth/+page.svelte`

여기에는 백엔드 테스트가 적용되지 않는다(순수 UI); 이 태스크는 계획 범위상 자동화 테스트가 없다 — 계획 끝부분의 수동 검증(Manual Verification) 섹션대로 검증할 것.

- [ ] **Step 1: 실패 사유 읽기**

`src/routes/auth/+page.svelte`의 기존 `onMount` 블록 안, `form`을 URL에서 이미 읽고 있는 부근(약 172번째 줄, `form = $page.url.searchParams.get('form');`)에 추가:

```ts
	let newapiSsoErrorReason = '';
```

최상위 `<script>` 변수로(기존 `let form` 선언 옆에), 그리고 `onMount` 안 기존 `form = $page.url.searchParams.get('form');` 줄 옆에:

```ts
		if ($page.url.searchParams.get('error') === 'newapi_sso_failed') {
			newapiSsoErrorReason = $page.url.searchParams.get('reason') ?? '';
		}
```

- [ ] **Step 2: 진입 버튼과 에러 메시지 추가**

다음과 같이 되어 있는 블록을 찾는다(약 288번째 줄, 381/446번째 줄에도 동일하게 반복됨):

```svelte
								{#if $config?.features.enable_login_form || $config?.features.enable_ldap || form}
```

그 조건문의 *첫 번째* 등장의 닫는 `{/if}` 바로 뒤에, (안에 중첩되는 게 아니라) 형제 블록으로 새 블록을 추가한다 — 그래야 로컬 폼이 보이든 안 보이든 상관없이 렌더링된다:

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

`invalid_client`는 별도 문구를 갖는다 — 배포 설정 오류이기 때문에, 다시 클릭해도 해결되지 않는다(design spec, 에러 처리 표). 그 외 모든 사유(`invalid_grant`, `network_error`, `invalid_userinfo`)는 new-api에서 새 링크를 받아 재시도하면 안전하게 해결된다.

- [ ] **Step 3: 수동 검증**

프론트엔드 개발 서버를 실행(`npm run dev`)하고, 백엔드 환경에 `ENABLE_NEWAPI_SSO=true`, `NEWAPI_ENTRY_URL=https://example.com/console/token`을 설정한 뒤 백엔드를 재시작해서 다음을 확인한다:
1. `/auth`를 방문하면 `NEWAPI_ENTRY_URL`을 가리키는 "Sign in with new-api" 링크가 보인다.
2. `ui.enable_login_form=false`도 함께 설정하면, 로컬 폼은 사라지고 new-api 링크는 여전히 보인다(이것이 실제 목표 배포 설정이다).
3. `/auth?error=newapi_sso_failed&reason=invalid_client`를 방문하면 "일시적으로 사용할 수 없음" 메시지가 보이고, `/auth?error=newapi_sso_failed&reason=invalid_grant`를 방문하면 "만료되었거나 이미 사용됨" 메시지가 보인다.

- [ ] **Step 4: 커밋**

```bash
git add src/routes/auth/+page.svelte
git commit -m "feat: show new-api sign-in entry point on the login page"
```

> 참고(최종 수정 반영): 최종 리뷰에서, 새로 추가된 `rate_limited`(Task 6/8 이후의 rate limiter 관련 수정에서 생긴 사유) 같은 새 리다이렉트 사유가 아무 특수 처리 없이 "링크가 만료되었거나 이미 사용됨"이라는 일반 문구로 떨어진다는 게 드러났다. 요청을 너무 많이 보낸 사용자에게 "다시 시도하면 된다"는 문구는 명백히 잘못된 안내이므로, `rate_limited`(그리고 `server_error`)에 대해서도 별도의 정확한 문구를 추가했다. 또한 이 페이지의 기존 범용 `toast.error(error)` 핸들러가 `newapi_sso_failed`에 대해서도 발동해 화면에 이미 보이는 친절한 인라인 메시지 옆에 `"newapi_sso_failed"`라는 날것의 문자열 토스트가 함께 뜨는 문제도 발견되어, 이 특정 에러 값에 대해서만 그 범용 토스트를 억제하도록 고쳤다.

---

### Task 11: `NEWAPI_RECONNECT_REQUIRED`에 대한 재연결 배너

**파일:**
- 수정: `src/lib/components/chat/Messages/ResponseMessage.svelte:878-879`
- 생성: `src/lib/components/chat/Messages/NewapiReconnectBanner.svelte`

백엔드 변경 없음 — Task 8이 이미 `main.py`의 기존 범용 채팅 에러 핸들러(`error_detail = e.detail if isinstance(e, HTTPException) else str(e)`, `backend/open_webui/main.py:1584-1607`)가 수정 없이 `{'type': 'chat:message:error', 'data': {'error': {'content': 'NEWAPI_RECONNECT_REQUIRED'}}}`를 내보내도록 만들었고, `Chat.svelte:1018-1019`가 이미 이를 `message.error`에 할당한다. 이 태스크는 오직 그 특정 마커가 어떻게 렌더링되는지만 바꾼다.

- [ ] **Step 1: 배너 컴포넌트 생성**

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

- [ ] **Step 2: `ResponseMessage.svelte`에서 마커를 특수 처리**

`src/lib/components/chat/Messages/ResponseMessage.svelte`에서 879번째 줄을 다음:

```svelte
								<Error content={message?.error?.content ?? message.content} />
```

에서 아래로 교체:

```svelte
								{#if message?.error?.content === 'NEWAPI_RECONNECT_REQUIRED'}
									<NewapiReconnectBanner />
								{:else}
									<Error content={message?.error?.content ?? message.content} />
								{/if}
```

파일의 `<script>` 블록 상단, 기존 `Error` 컴포넌트 import 옆에(`grep -n "import Error from" src/lib/components/chat/Messages/ResponseMessage.svelte`로 찾을 것) import를 추가:

```ts
	import NewapiReconnectBanner from './NewapiReconnectBanner.svelte';
```

- [ ] **Step 3: 수동 검증**

`newapi_session` 모드 커넥션을 설정하고 현재 사용자에 대한 `OAuthSessions` 행이 없는(또는 만료된 — Task 8 참고) 상태에서 채팅 메시지를 보내, 일반 빨간 에러 대신 노란 재연결 배너가 뜨고 그 링크가 `NEWAPI_ENTRY_URL`을 가리키는지 확인한다.

- [ ] **Step 4: 커밋**

```bash
git add src/lib/components/chat/Messages/NewapiReconnectBanner.svelte src/lib/components/chat/Messages/ResponseMessage.svelte
git commit -m "feat: show a reconnect banner instead of a generic error on NEWAPI_RECONNECT_REQUIRED"
```

---

## 수동 검증 (이 저장소 안에서 자동화할 수 없음)

design spec의 "자동화 불가 영역" 기준: 실서비스 전환 전, (이 계획의 목보다는) 실제 new-api 인스턴스를 대상으로 다음을 확인할 것:
1. new-api 쪽에 등록된 `redirect_uri`가 이 배포의 `/auth/newapi/callback` URL과 정확히 일치하는지.
2. 실제 인증 코드가 한 번 사용된 뒤 두 번째 시도에서 `invalid_grant`로 거부되는지.
3. new-api에서 로그아웃하면 이미 열려 있는 OpenWebUI 탭에서 *다음* 채팅 요청이 재연결 배너를 보여주는지(24시간/즉시 무효화 계약을 OpenWebUI 쪽만이 아니라 종단간으로 검증).
4. new-api 쪽의 "Open in Chat App" 링크가 (new-api 팀에 전달한 링크 처리 요구사항대로) 그냥 복사 가능한 URL로 렌더링되지 않는지.

## 배포 체크리스트

이 기능을 켤 때 함께 설정할 것:

```
ENABLE_NEWAPI_SSO=true
NEWAPI_OAUTH_BASE_URL=https://<newapi-host>
NEWAPI_OAUTH_CLIENT_ID=<new-api 운영팀에서 발급>
NEWAPI_OAUTH_CLIENT_SECRET=<new-api 운영팀에서 발급>
NEWAPI_ENTRY_URL=<new-api 로그인/진입 화면으로 돌아가는 URL>
ENABLE_PASSWORD_AUTH=false
```

(이번 배치의 M7 수정 이후, `ENABLE_NEWAPI_SSO=true`인 상태에서 네 개의 `NEWAPI_*` 값 중 하나라도
비어 있거나 — 또는 `ENABLE_PASSWORD_AUTH`가 기본값 `true`로 남아있는 채로 함께 켜져 있으면 —
`backend/open_webui/config.py`가 무엇이 빠졌는지/여전히 켜져 있는지 이름을 명시한 시작 시점
`WARNING` 로그를 남긴다. 아무것도 자동으로 고쳐지지 않으니, 이 경고는 체크리스트를 대신하는 게
아니라 체크리스트를 상기시키는 용도로만 볼 것.)

그리고 관리자 설정 API / `Config.upsert`를 통해:
```
ui.enable_login_form = false
ui.enable_signup = false
auth.enable_api_keys = false
ui.enable_user_management = false
auth.jwt_expiry = 24h
ui.default_user_role = user
```

(`ui.enable_password_change_form`은 별도로 바꿀 필요 없다 — `Account.svelte`는 `enable_login_form`도 true일 때만 그 탭을 보여주므로, 위 첫 번째 플래그에 의해 이미 숨겨진다.)

- **`ui.default_user_role = user`가 여기서 중요한 이유.** `_provision_or_login_user`
  (`backend/open_webui/routers/newapi_sso.py`)는 모든 신규 SSO 사용자를
  `role=await Config.get('ui.default_user_role')`로 생성한다 — new-api 전용 값이 아니라
  플랫폼의 일반 신규가입 기본값이다. 이 설정이 더 엄격한 값(예: `pending`)으로 남아 있으면,
  new-api를 통해 성공적으로 인증한 사용자가 — 이 연동은 그 사용자를 완전히 신뢰할 대상으로
  간주하는데도 — 여전히 잠긴 채로 남아, "new-api를 통과한 사용자는 무조건 허용"이라는 이 기능의
  의도와 모순된다. 이 체크리스트의 일부로 반드시 `user`로 설정할 것.

- **`oauth.merge_accounts_by_email`.** `_provision_or_login_user`는 (표준 OAuth 콜백의
  `OAUTH_MERGE_ACCOUNTS_BY_EMAIL` 동작을 `utils/oauth.py`에서 그대로 따라) 아직 `newapi` oauth
  sub가 기록되지 않은, 이메일이 일치하는 기존 계정에 new-api 로그인을 연결하기 전에 이 플래그를
  확인한다. `true`이면 이메일이 같은 기존 로컬/다른 프로바이더 계정을 그대로 가져와 연결하고,
  `false`이면 항상 새 계정을 새로 만든다(어느 쪽이든 new-api는 여전히 신뢰되는 대상이다 — 이
  플래그는 오직 기존 신원에 병합을 허용할지만 결정한다). 배포별로 기존 계정을 이메일로 병합할지,
  분리해서 유지할지를 의도적으로 결정할 것.

- **커넥션에 `auth_type: 'newapi_session'` 설정하기 (해당 UI 없음).**
  사용자별 new-api 토큰 전달(Task 8)은 저장된 설정에 `"auth_type": "newapi_session"`이 있는
  OpenAI 커넥션에서만 활성화된다. `AddConnectionModal.svelte`의 Auth 드롭다운에서는 이 값을
  선택할 방법이 없다 — 그 옵션은 (`<option>` 주변의 `{#if auth_type === 'newapi_session'}` 가드
  때문에) 이미 설정되어 있을 때만 렌더링된다 — 그래서 반드시 `POST /openai/config/update`
  (`backend/open_webui/routers/openai.py`)를 통해 직접 설정해야 한다. 그 엔드포인트는
  `OPENAI_API_BASE_URLS`/`OPENAI_API_KEYS`/`OPENAI_API_CONFIGS`를 통째로 **교체**하므로, 먼저
  관리자로 `GET /openai/config`를 호출해 기존 배열을 확인하고 그 위에 패치할 것 — 커넥션 하나짜리
  페이로드만 보내면 이미 설정된 다른 커넥션들이 조용히 사라진다. `OPENAI_API_CONFIGS`는 병렬
  배열인 `OPENAI_API_BASE_URLS`/`OPENAI_API_KEYS`의 *문자열 인덱스*로 키가 잡힌다(예: 첫 번째
  커넥션이면 `"0"`), 이름이나 URL이 아니다. 예시(new-api 커넥션이 이미 인덱스 `0`에 있고 다른
  커넥션은 아직 없다고 가정):

  ```bash
  curl -X POST "$WEBUI_BASE_URL/openai/config/update" \
    -H "Authorization: Bearer $ADMIN_TOKEN" \
    -H "Content-Type: application/json" \
    -d '{
      "ENABLE_OPENAI_API": true,
      "OPENAI_API_BASE_URLS": ["https://<newapi-host>/v1"],
      "OPENAI_API_KEYS": [""],
      "OPENAI_API_CONFIGS": {
        "0": {
          "auth_type": "newapi_session"
        }
      }
    }'
  ```

  그 인덱스의 API 키는 `""`로 남겨둬도 된다 — `auth_type: newapi_session`이 설정되어 있으면
  백엔드가 공유 키 대신 (`newapi_callback`이 채워 넣는) `OAuthSessions`에서 각 사용자 본인의
  저장된 new-api OAuth 토큰을 조회한다.

- **`WEBUI_AUTH_COOKIE_SAME_SITE`.** 배포에서 이 값을 `strict`로 설정하면,
  `newapi_callback`이 의존하는 교차 내비게이션 리다이렉트 체인(new-api → `/auth/newapi/callback`
  → `Set-Cookie: token=...`이 실린 `/auth`)에서, 브라우저가 이를 교차 사이트 최상위 내비게이션으로
  간주해 `oauthCallbackHandler()`(`src/routes/auth/+page.svelte`)가 `document.cookie`로 그 값을
  읽기도 전에 쿠키가 버려질 수 있다. new-api SSO 배포에서는 기본값(`lax`)을 유지할 것 — `strict`는
  이 흐름에서 지원되지 않는다.

- **알려진 한계: `redirectPath`가 new-api SSO를 거치는 동안 보존되지 않는다.**
  표준 로그인 폼과 일반 OAuth 흐름에서는 `/auth`의 `?redirect=`가 `localStorage.redirectPath`에
  보관됐다가(`src/routes/auth/+page.svelte`의 `onMount` 핸들러 참고) 로그인 후 소비되어 사용자를
  특정 채팅 화면으로 되돌려 보낸다. new-api의 IdP-initiated 흐름에는 이에 대응하는 장치가 없다 —
  new-api 쪽 "Open in Chat App" 링크는 OpenWebUI가 되돌려 받을 리다이렉트 목적지를 전혀 실어 보내지
  않고, `newapi_callback`은 항상 순수 `/auth`로 리다이렉트한다. 특정 채팅을 열어 두고 있던 사용자는
  (애초에 new-api 쪽에서 이런 진입 경로를 지원한다면) SSO 로그인 후 그 채팅이 아니라 `/`로 이동하게
  된다. 이는 지금 이 연동에서 받아들이는 한계이며, 여기서 고칠 버그가 아니다 — 제대로 해결하려면
  new-api 쪽 링크 생성 로직 자체가 리다이렉트 목적지를 실어서 왕복시켜야 하는데, 이는 이 코드베이스가
  통제할 수 있는 범위 밖이다.

---

## 부록: 최종 전체 브랜치 리뷰와 마무리 수정 요약

이 문서의 각 태스크 본문은 **최초 작성된 계획 그대로**를 보존하고 있으며(구현 시점의 원래 의도를
기록으로 남기기 위해), 그 아래 "참고(최종 수정 반영)" 콜아웃에 실제 최종 코드와 달라진 부분을
표시해 두었다. 실제 구현은 11개 태스크가 모두 완료된 뒤, 전체 브랜치를 대상으로 한 별도의 최종
리뷰를 거쳤고, 거기서 다음이 추가로 발견되어 수정됐다(각 항목은 태스크별 리뷰만으로는 잡아낼 수
없는, 여러 태스크의 경계에서만 드러나는 문제였다):

- **C1 (Critical):** 로그인 성공 후 채팅 화면에 진입하지 못하는 문제 (httpOnly 쿠키 + `/` 리다이렉트) — Task 4 콜백 수정.
- **I1:** new-api 쪽에서 로그아웃해 토큰이 실제로 폐기됐을 때 재연결 배너가 뜨지 않는 문제 — Task 8 관련 `openai.py`의 채팅 완료 경로 수정.
- **I2:** `newapi_oauth.py`의 예외 처리 범위 확장, `newapi_sso.py` 콜백에 최종 catch-all과 빈 code 가드 추가.
- **I3:** `utils/middleware.py`/`functions.py`의 "쿠키 없을 때 최근 세션 사용" 폴백이 new-api 자격증명을 유출하거나 삭제할 수 있는 문제 — 두 파일 모두에 `newapi` 제외 필터 추가(하나는 이미 있던 `mcp:` 가드 확장, 하나는 그 가드 자체가 아예 없어서 함께 추가).
- **I4:** 이메일 fallback 계정 연결을 `oauth.merge_accounts_by_email` 뒤로 게이팅.
- **I5:** 콜백 라우트를 `ENABLE_NEWAPI_SSO`로 게이팅하고 요청 빈도 제한 추가(추가로, 그 제한기가 기존 `auths.py`의 제한기와 키가 겹치는 문제도 발견되어 네임스페이스를 분리).
- **I6:** `AddConnectionModal.svelte`가 `newapi_session` 커넥션을 열었을 때 조용히 `bearer`로 다운그레이드되는 문제 방지.
- **M1-M9 및 사전에 지연시켰던 3개 항목:** i18n 추출, 시작 시점 설정 검증 경고, 이메일 소문자 정규화, 시간 소스 일관성, 미사용 import 제거, 사용자 2명 간 토큰 격리 테스트, 로그인 페이지 문구 개선, 배포 체크리스트 보강, 그리고 마무리 단계에서 CI 포맷팅 게이트 통과 및 `utils/oauth.py`의 `get_oauth_token`에도 동일한 `newapi` 제외 가드를 대칭적으로 추가(3중 방어).

모든 항목은 각각 별도의 리뷰어가 소스 코드를 직접 읽고 테스트를 실행해 검증한 뒤 승인됐으며,
최종적으로 전체 48~49개의 백엔드 테스트가 통과하는 상태로 브랜치가 병합 가능한 상태에 도달했다.
