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
