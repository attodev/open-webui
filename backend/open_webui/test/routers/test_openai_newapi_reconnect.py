"""
Finding I1 (final whole-branch review): when a new-api-backed connection
uses auth_type='newapi_session' and the LOCALLY stored token still looks
fresh (its recorded expires_at is in the future) but new-api has actually
revoked/logged out the session on ITS side, the outbound request goes out
anyway and new-api answers with a real upstream HTTP 401.

Nothing previously mapped that upstream 401 (for a newapi_session
connection specifically) to the NEWAPI_RECONNECT_REQUIRED marker that the
frontend's ResponseMessage.svelte looks for to show the reconnect banner
-- the caller just saw the passthrough 401 instead.

This test drives the real /openai/chat/completions route end-to-end (only
the outbound aiohttp call and the connection lookup are mocked) and
asserts the client-visible response is the 424/NEWAPI_RECONNECT_REQUIRED
marker, not a passthrough 401 (and not a 500 -- see the module docstring
in openai.py's generate_chat_completion about except-clause ordering).
"""

import time
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport
from open_webui.internal.db import AsyncSessionLocal
from open_webui.models.config import Config
from open_webui.models.oauth_sessions import OAuthSessions
from open_webui.models.users import UserModel
from open_webui.utils.auth import get_verified_user


class _FakeAiohttpResponse:
    """Minimal stand-in for an aiohttp.ClientResponse used by generate_chat_completion."""

    def __init__(self, status, json_body, content_type='application/json'):
        self.status = status
        self.headers = {'Content-Type': content_type}
        self._json_body = json_body
        self.closed = False

    async def json(self, *args, **kwargs):
        return self._json_body

    async def text(self):
        import json as _json

        return _json.dumps(self._json_body)

    def close(self):
        # aiohttp 3.9+ ClientResponse.close() is synchronous.
        self.closed = True
        return None


def _mount(app):
    from open_webui.routers import openai as openai_router

    app.include_router(openai_router.router, prefix='/openai', tags=['openai'])


def _fake_user(id='user-1'):
    # Role is 'admin' purely so check_model_access's unregistered-model
    # branch (utils/access_control/__init__.py) doesn't 403 -- this test's
    # model_id is never inserted into the Models table on purpose (it only
    # needs to exist in app.state.OPENAI_MODELS to resolve a connection),
    # and that access-control policy is out of scope for finding I1.
    return UserModel(
        id=id,
        name='Test',
        email='test@example.com',
        role='admin',
        last_active_at=0,
        updated_at=0,
        created_at=0,
    )


@pytest.mark.asyncio
async def test_chat_completion_maps_upstream_401_to_reconnect_marker_for_newapi_session(
    db_engine, async_client, monkeypatch
):
    user = _fake_user()

    app = FastAPI()
    _mount(app)
    app.dependency_overrides[get_verified_user] = lambda: user
    async_client._transport = ASGITransport(app=app)

    # openai.enable gates the whole endpoint; default with no DB row/DEFAULTS
    # entry is falsy, so this connection would 503 before ever reaching the
    # upstream call.
    monkeypatch.setitem(Config.DEFAULTS, 'openai.enable', True)

    # The model is already resolved in the app-state cache, at urlIdx 0, so
    # get_all_models() (which would try real network calls) is never invoked.
    app.state.OPENAI_MODELS = {'gpt-test': {'id': 'gpt-test', 'urlIdx': 0}}

    # Local OAuthSessions row: NOT expired per our own clock (expires_at is
    # comfortably in the future) -- this is exactly the case get_headers_and_
    # cookies' own expiry check does NOT catch, because new-api revoked the
    # session on ITS side, not ours.
    async with AsyncSessionLocal() as db:
        await OAuthSessions.create_session(
            user.id, 'newapi', {'access_token': 'sk-looks-fresh', 'expires_at': int(time.time()) + 3600}, db=db
        )

    fake_response = _FakeAiohttpResponse(
        status=401,
        json_body={'error': {'message': 'Unauthorized', 'type': 'invalid_request_error'}},
    )
    fake_session = AsyncMock()
    fake_session.request = AsyncMock(return_value=fake_response)

    with (
        patch(
            'open_webui.routers.openai.get_openai_connection',
            new=AsyncMock(
                return_value=(
                    'https://newapi.example.com/v1',
                    'unused-admin-key',
                    {'auth_type': 'newapi_session'},
                )
            ),
        ),
        patch('open_webui.routers.openai.get_session', new=AsyncMock(return_value=fake_session)),
    ):
        res = await async_client.post(
            '/openai/chat/completions',
            json={'model': 'gpt-test', 'messages': [{'role': 'user', 'content': 'hi'}]},
        )

    assert res.status_code == 424, res.text
    assert res.json()['detail'] == 'NEWAPI_RECONNECT_REQUIRED'


@pytest.mark.asyncio
async def test_chat_completion_maps_upstream_401_to_reconnect_marker_when_sse_content_type(
    db_engine, async_client, monkeypatch
):
    """
    Same scenario as the test above, but the upstream returned the error
    with a `text/event-stream` Content-Type -- generate_chat_completion has
    a SEPARATE branch for that case (it reads the body and returns a
    JSONResponse/PlainTextResponse instead of streaming the error back).
    Both branches need the newapi_session/401 -> 424 mapping independently.
    """
    user = _fake_user()

    app = FastAPI()
    _mount(app)
    app.dependency_overrides[get_verified_user] = lambda: user
    async_client._transport = ASGITransport(app=app)

    monkeypatch.setitem(Config.DEFAULTS, 'openai.enable', True)
    app.state.OPENAI_MODELS = {'gpt-test': {'id': 'gpt-test', 'urlIdx': 0}}

    async with AsyncSessionLocal() as db:
        await OAuthSessions.create_session(
            user.id, 'newapi', {'access_token': 'sk-looks-fresh', 'expires_at': int(time.time()) + 3600}, db=db
        )

    fake_response = _FakeAiohttpResponse(
        status=401,
        json_body={'error': {'message': 'Unauthorized', 'type': 'invalid_request_error'}},
        content_type='text/event-stream',
    )
    fake_session = AsyncMock()
    fake_session.request = AsyncMock(return_value=fake_response)

    with (
        patch(
            'open_webui.routers.openai.get_openai_connection',
            new=AsyncMock(
                return_value=(
                    'https://newapi.example.com/v1',
                    'unused-admin-key',
                    {'auth_type': 'newapi_session'},
                )
            ),
        ),
        patch('open_webui.routers.openai.get_session', new=AsyncMock(return_value=fake_session)),
    ):
        res = await async_client.post(
            '/openai/chat/completions',
            json={'model': 'gpt-test', 'stream': True, 'messages': [{'role': 'user', 'content': 'hi'}]},
        )

    assert res.status_code == 424, res.text
    assert res.json()['detail'] == 'NEWAPI_RECONNECT_REQUIRED'


@pytest.mark.asyncio
async def test_chat_completion_passes_through_401_for_non_newapi_session_connections(
    db_engine, async_client, monkeypatch
):
    """
    Sanity companion: a plain bearer-key connection getting a 401 from its
    provider must still see the ordinary passthrough error, not be
    mistakenly upgraded to the newapi reconnect marker.
    """
    user = _fake_user()

    app = FastAPI()
    _mount(app)
    app.dependency_overrides[get_verified_user] = lambda: user
    async_client._transport = ASGITransport(app=app)

    monkeypatch.setitem(Config.DEFAULTS, 'openai.enable', True)
    app.state.OPENAI_MODELS = {'gpt-test': {'id': 'gpt-test', 'urlIdx': 0}}

    fake_response = _FakeAiohttpResponse(
        status=401,
        json_body={'error': {'message': 'Invalid API key', 'type': 'invalid_request_error'}},
    )
    fake_session = AsyncMock()
    fake_session.request = AsyncMock(return_value=fake_response)

    with (
        patch(
            'open_webui.routers.openai.get_openai_connection',
            new=AsyncMock(return_value=('https://api.example.com/v1', 'sk-bad-key', {'auth_type': 'bearer'})),
        ),
        patch('open_webui.routers.openai.get_session', new=AsyncMock(return_value=fake_session)),
    ):
        res = await async_client.post(
            '/openai/chat/completions',
            json={'model': 'gpt-test', 'messages': [{'role': 'user', 'content': 'hi'}]},
        )

    assert res.status_code == 401, res.text
    assert res.json()['error']['message'] == 'Invalid API key'
