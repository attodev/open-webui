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
