import time

import pytest
from open_webui.internal.db import AsyncSessionLocal
from open_webui.models.oauth_sessions import OAuthSessions
from open_webui.models.users import Users
from open_webui.utils.oauth import OAuthManager


@pytest.mark.asyncio
async def test_get_oauth_token_excludes_newapi_provider_session(db_engine):
    """
    Defense-in-depth regression test for finding I3: OAuthManager.get_oauth_token
    already bails out early for `mcp:`-prefixed provider sessions (#24618) so it
    never attempts to refresh a session it doesn't own. The new-api SSO session
    is the same kind of foreign, per-user credential — not a general OAuth token
    this manager should refresh/use — so the same early-return guard must also
    cover provider == 'newapi', mirroring the exclusion already applied at the
    other two fallback-lookup call sites (functions.py, utils/middleware.py).
    """
    async with AsyncSessionLocal() as db:
        user = await Users.insert_new_user(
            id='user-newapi-token', name='N', email='n-token@example.com', role='user', db=db
        )
        session = await OAuthSessions.create_session(
            user.id, 'newapi', {'access_token': 'sk-newapi', 'expires_at': int(time.time()) + 3600}, db=db
        )

    manager = OAuthManager(app=None)

    result = await manager.get_oauth_token(user.id, session.id)

    assert result is None
