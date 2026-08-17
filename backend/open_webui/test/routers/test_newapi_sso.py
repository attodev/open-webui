import logging
import time
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport
from open_webui.internal.db import AsyncSessionLocal
from open_webui.models.users import Users
from open_webui.utils.newapi_oauth import NewapiOAuthError


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
