import datetime
import time

import pytest
from open_webui.models.users import Users


@pytest.mark.asyncio
async def test_create_session_response_honors_expires_delta_override(async_db):
    from fastapi import Request
    from open_webui.internal.db import AsyncSessionLocal
    from open_webui.routers.auths import create_session_response
    from open_webui.utils.auth import decode_token

    async with AsyncSessionLocal() as db:
        user = await Users.insert_new_user(
            id='user-1', name='Test User', email='test@example.com', role='user', db=db
        )

        from fastapi import FastAPI

        app = FastAPI()
        scope = {'type': 'http', 'method': 'GET', 'path': '/', 'headers': [], 'app': app}
        request = Request(scope)

        before = int(time.time())
        result = await create_session_response(
            request, user, db, expires_delta=datetime.timedelta(hours=24)
        )

        decoded = decode_token(result['token'])
        assert decoded['exp'] - before == pytest.approx(24 * 3600, abs=5)
        assert result['expires_at'] - before == pytest.approx(24 * 3600, abs=5)
