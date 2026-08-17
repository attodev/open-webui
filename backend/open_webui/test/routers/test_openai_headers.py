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
        id=id,
        name='Test',
        email='test@example.com',
        role='user',
        last_active_at=0,
        updated_at=0,
        created_at=0,
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
async def test_newapi_session_auth_type_raises_when_access_token_missing_from_stored_token(db_engine):
    """
    Defensive case for finding M2: a session row can exist and not be
    expired, but its stored token dict might be missing 'access_token'
    (shouldn't normally happen). Previously the code fell through and sent
    the request with no Authorization header at all instead of raising
    the 424 reconnect signal.
    """
    from open_webui.routers.openai import get_headers_and_cookies

    user = _fake_user()
    async with AsyncSessionLocal() as db:
        await OAuthSessions.create_session(user.id, 'newapi', {'expires_at': int(time.time()) + 3600}, db=db)

    with pytest.raises(HTTPException) as exc_info:
        await get_headers_and_cookies(
            _fake_request(), 'https://newapi.example.com/v1', config={'auth_type': 'newapi_session'}, user=user
        )

    assert exc_info.value.status_code == 424
    assert exc_info.value.detail == 'NEWAPI_RECONNECT_REQUIRED'


@pytest.mark.asyncio
async def test_newapi_session_auth_type_isolates_tokens_between_users(db_engine):
    """
    Deferred finding #5: each user's newapi_session token is per-user
    billing/gateway credential — user A must never see user B's token
    (and vice versa) when both have their own stored OAuthSessions row.
    """
    from open_webui.routers.openai import get_headers_and_cookies

    user_a = _fake_user(id='user-a')
    user_b = _fake_user(id='user-b')

    async with AsyncSessionLocal() as db:
        await OAuthSessions.create_session(
            user_a.id, 'newapi', {'access_token': 'sk-user-a', 'expires_at': int(time.time()) + 3600}, db=db
        )
        await OAuthSessions.create_session(
            user_b.id, 'newapi', {'access_token': 'sk-user-b', 'expires_at': int(time.time()) + 3600}, db=db
        )

    headers_a, _ = await get_headers_and_cookies(
        _fake_request(), 'https://newapi.example.com/v1', config={'auth_type': 'newapi_session'}, user=user_a
    )
    headers_b, _ = await get_headers_and_cookies(
        _fake_request(), 'https://newapi.example.com/v1', config={'auth_type': 'newapi_session'}, user=user_b
    )

    assert headers_a['Authorization'] == 'Bearer sk-user-a'
    assert headers_b['Authorization'] == 'Bearer sk-user-b'
    assert headers_a['Authorization'] != headers_b['Authorization']


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
