import time
from unittest.mock import AsyncMock

import pytest
from open_webui.internal.db import AsyncSessionLocal
from open_webui.models.oauth_sessions import OAuthSessions
from open_webui.models.users import Users


class _FakeOAuthManager:
    def __init__(self):
        self.get_oauth_token = AsyncMock(return_value={'access_token': 'should-not-be-used'})


class _FakeAppState:
    def __init__(self, oauth_manager):
        self.oauth_manager = oauth_manager


class _FakeApp:
    def __init__(self, oauth_manager):
        self.state = _FakeAppState(oauth_manager)


class _FakeRequest:
    """Minimal stand-in for fastapi.Request: only `.cookies` and `.app.state` are read."""

    def __init__(self, app):
        self.cookies = {}
        self.app = app


@pytest.mark.asyncio
async def test_get_system_oauth_token_excludes_newapi_provider_session(db_engine):
    """
    Regression test for finding I3: the new-api SSO session is a
    per-user LLM-gateway billing credential, not a general-purpose OAuth
    token — it must never be picked up by the cookie-less "most recent
    OAuth session" fallback that pipe/filter functions and automations
    use. A user with ONLY a `newapi`-provider session (no cookie, no other
    sessions) must get back None, not an attempt to use that session.
    """
    from open_webui.utils.middleware import get_system_oauth_token

    async with AsyncSessionLocal() as db:
        user = await Users.insert_new_user(id='user-newapi-only', name='N', email='n@example.com', role='user', db=db)
        await OAuthSessions.create_session(
            user.id, 'newapi', {'access_token': 'sk-newapi', 'expires_at': int(time.time()) + 3600}, db=db
        )

    oauth_manager = _FakeOAuthManager()
    request = _FakeRequest(_FakeApp(oauth_manager))

    result = await get_system_oauth_token(request, user)

    assert result is None
    oauth_manager.get_oauth_token.assert_not_called()
