import logging
import time
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport
from open_webui.internal.db import AsyncSessionLocal
from open_webui.models.config import Config
from open_webui.models.users import Users
from open_webui.utils.newapi_oauth import NewapiOAuthError
from open_webui.utils.rate_limit import RateLimiter


def _mount(app):
    from open_webui.routers import newapi_sso

    app.include_router(newapi_sso.router, prefix='/auth/newapi')


@pytest.fixture(autouse=True)
def _enable_newapi_sso(monkeypatch):
    """
    ENABLE_NEWAPI_SSO defaults to False (env var unset in tests), but every
    test in this file below exercises the callback's actual behavior, so
    default it on here. Patched on the newapi_sso module's own namespace
    (not just open_webui.env) since it's imported by value at module load
    time. The one test that cares about the disabled case re-patches this
    to False itself.

    Also resets the rate limiter's in-memory fallback store: there's no
    real Redis in tests, and every request in this file shares the same
    'unknown' key (no real client IP under ASGITransport), so counts would
    otherwise accumulate across tests and trip the limiter.
    """
    monkeypatch.setattr('open_webui.routers.newapi_sso.ENABLE_NEWAPI_SSO', True)
    RateLimiter._memory_store.clear()


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


@pytest.mark.asyncio
async def test_callback_success_cookie_is_readable_by_frontend_js(db_engine, async_client):
    """
    Regression test for the bug where a successful login never reached the
    chat UI: this app is a pure SPA that only bootstraps `$user` from
    `localStorage.token`, which `oauthCallbackHandler()`
    (src/routes/auth/+page.svelte) populates by reading the `token` cookie
    via `document.cookie` — invisible if the cookie is httpOnly. The
    redirect must also land on `/auth` (where that handler runs), not `/`
    (whose unauthenticated-route guard would bounce the user out before the
    cookie is ever read).
    """
    app = FastAPI()
    _mount(app)
    async_client._transport = ASGITransport(app=app)

    with (
        patch(
            'open_webui.routers.newapi_sso.exchange_code_for_token',
            new=AsyncMock(return_value={'access_token': 'sk-frank', 'expires_in': 86400}),
        ),
        patch(
            'open_webui.routers.newapi_sso.fetch_userinfo',
            new=AsyncMock(
                return_value={'sub': 'newapi-user-frank', 'email': 'frank@example.com', 'name': 'Frank', 'is_admin': False}
            ),
        ),
    ):
        res = await async_client.get('/auth/newapi/callback?code=abc123', follow_redirects=False)

    assert res.status_code == 302
    assert res.headers['location'] == '/auth'

    set_cookie_headers = res.headers.get_list('set-cookie')
    token_cookie_headers = [h for h in set_cookie_headers if h.startswith('token=')]
    assert len(token_cookie_headers) == 1
    assert 'httponly' not in token_cookie_headers[0].lower()


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
async def test_callback_matches_existing_local_account_by_email_when_no_sub_link(db_engine, async_client, monkeypatch):
    """
    Email-fallback linking only happens when oauth.merge_accounts_by_email
    is enabled (finding I4) — mirrors the standard OAuth callback's own
    check in utils/oauth.py. The flag defaults to False, so this "on" case
    has to enable it explicitly.
    """
    monkeypatch.setitem(Config.DEFAULTS, 'oauth.merge_accounts_by_email', True)

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
async def test_callback_creates_new_user_instead_of_linking_when_merge_by_email_disabled(db_engine, async_client):
    """
    Companion to the "on" case above: with oauth.merge_accounts_by_email
    left at its default (False), a sub-less new-api login whose email
    matches an existing account must NOT be linked into that account — it
    should fall through to creating a brand-new user, same as if there had
    been no email match at all.

    The existing account's email uses different casing from the trip
    userinfo claim (both refer to "the same" address once lowercased) so
    that both rows can coexist under the DB's case-sensitive unique
    constraint on email while still exercising Users.get_user_by_email's
    case-insensitive match — that's the exact condition finding I4 gates.
    """
    app = FastAPI()
    _mount(app)
    async_client._transport = ASGITransport(app=app)

    async with AsyncSessionLocal() as db:
        await Users.insert_new_user(
            id='user-dana', name='Dana', email='Dana@Example.com', role='user', db=db
        )

    with (
        patch(
            'open_webui.routers.newapi_sso.exchange_code_for_token',
            new=AsyncMock(return_value={'access_token': 'sk-dana2', 'expires_in': 86400}),
        ),
        patch(
            'open_webui.routers.newapi_sso.fetch_userinfo',
            new=AsyncMock(
                return_value={
                    'sub': 'newapi-user-dana2',
                    'email': 'dana@example.com',
                    'name': 'Dana Two',
                    'is_admin': False,
                }
            ),
        ),
    ):
        res = await async_client.get('/auth/newapi/callback?code=abc123', follow_redirects=False)

    assert res.status_code == 302
    assert res.headers['location'] == '/auth'

    async with AsyncSessionLocal() as db:
        assert await Users.get_num_users(db=db) == 2

        new_user = await Users.get_user_by_oauth_sub('newapi', 'newapi-user-dana2', db=db)
        assert new_user is not None
        assert new_user.id != 'user-dana'

        old_user = await Users.get_user_by_id('user-dana', db=db)
        assert not (old_user.oauth or {}).get('newapi')


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


@pytest.mark.asyncio
async def test_callback_redirects_to_server_error_on_concurrent_new_user_race(db_engine, async_client):
    """
    Two concurrent IdP-initiated logins for the same brand-new email (e.g. a
    double-clicked "Open in Chat" link) can both pass the
    `Users.get_user_by_email` pre-check before either commits, then both
    attempt `Auths.insert_new_auth` — the loser trips the unique-email
    constraint as a `sqlalchemy.exc.IntegrityError`.

    We simulate the loser's request deterministically: a user with the
    target email is already committed (standing in for the winner's
    request, which finished first), and `Users.get_user_by_email` is forced
    to still report "no such user" — exactly what it would have returned at
    the moment the loser's request actually ran that check, before the
    winner committed. That forces this request down the "create new user"
    path against an email that the database will now reject.
    """
    app = FastAPI()
    _mount(app)
    async_client._transport = ASGITransport(app=app)

    async with AsyncSessionLocal() as db:
        await Users.insert_new_user(id='user-racer-winner', name='Erin', email='erin@example.com', role='user', db=db)

    with (
        patch(
            'open_webui.routers.newapi_sso.exchange_code_for_token',
            new=AsyncMock(return_value={'access_token': 'sk-erin', 'expires_in': 86400}),
        ),
        patch(
            'open_webui.routers.newapi_sso.fetch_userinfo',
            new=AsyncMock(
                return_value={'sub': 'newapi-user-erin', 'email': 'erin@example.com', 'name': 'Erin', 'is_admin': False}
            ),
        ),
        patch('open_webui.routers.newapi_sso.Users.get_user_by_email', new=AsyncMock(return_value=None)),
    ):
        res = await async_client.get('/auth/newapi/callback?code=abc123', follow_redirects=False)

    assert res.status_code == 302
    assert res.headers['location'] == '/auth?error=newapi_sso_failed&reason=server_error'
    assert 'token' not in res.cookies

    async with AsyncSessionLocal() as db:
        # Only the winner's account exists — the loser's request never created a duplicate.
        assert await Users.get_num_users(db=db) == 1


@pytest.mark.asyncio
async def test_callback_redirects_gracefully_when_code_is_missing(db_engine, async_client):
    app = FastAPI()
    _mount(app)
    async_client._transport = ASGITransport(app=app)

    mock_exchange = AsyncMock()
    with patch('open_webui.routers.newapi_sso.exchange_code_for_token', new=mock_exchange):
        res = await async_client.get('/auth/newapi/callback', follow_redirects=False)

    assert res.status_code == 302
    assert res.headers['location'] == '/auth?error=newapi_sso_failed&reason=missing_code'
    mock_exchange.assert_not_called()


@pytest.mark.asyncio
async def test_callback_redirects_gracefully_when_code_is_empty(db_engine, async_client):
    app = FastAPI()
    _mount(app)
    async_client._transport = ASGITransport(app=app)

    mock_exchange = AsyncMock()
    with patch('open_webui.routers.newapi_sso.exchange_code_for_token', new=mock_exchange):
        res = await async_client.get('/auth/newapi/callback?code=', follow_redirects=False)

    assert res.status_code == 302
    assert res.headers['location'] == '/auth?error=newapi_sso_failed&reason=missing_code'
    mock_exchange.assert_not_called()


@pytest.mark.asyncio
async def test_callback_redirects_gracefully_on_unexpected_exception_during_provisioning(db_engine, async_client):
    """
    A catch-all for unexpected exceptions inside the provisioning try-block
    must never let a raw 500 reach the browser — every failure in this
    route redirects to /auth with a reason.
    """
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
            new=AsyncMock(
                return_value={'sub': 'newapi-user-x', 'email': 'x@example.com', 'name': 'X', 'is_admin': False}
            ),
        ),
        patch(
            'open_webui.routers.newapi_sso._provision_or_login_user',
            new=AsyncMock(side_effect=ValueError('boom')),
        ),
    ):
        res = await async_client.get('/auth/newapi/callback?code=abc123', follow_redirects=False)

    assert res.status_code == 302
    assert res.headers['location'] == '/auth?error=newapi_sso_failed&reason=server_error'
    assert 'token' not in res.cookies


@pytest.mark.asyncio
async def test_callback_redirects_without_token_exchange_when_sso_disabled(db_engine, async_client, monkeypatch):
    """
    ENABLE_NEWAPI_SSO gates this route the same way it gates /api/config's
    newapi_sso.enable — the router is registered unconditionally in
    main.py, so this check has to live in the handler itself. Patched on
    the newapi_sso module's own namespace (not just open_webui.env) since
    it's imported by value at module load time.
    """
    app = FastAPI()
    _mount(app)
    async_client._transport = ASGITransport(app=app)

    monkeypatch.setattr('open_webui.routers.newapi_sso.ENABLE_NEWAPI_SSO', False)

    mock_exchange = AsyncMock()
    with patch('open_webui.routers.newapi_sso.exchange_code_for_token', new=mock_exchange):
        res = await async_client.get('/auth/newapi/callback?code=abc123&state=xyz', follow_redirects=False)

    assert res.status_code == 302
    mock_exchange.assert_not_called()


@pytest.mark.asyncio
async def test_callback_redirects_gracefully_on_unexpected_exception_during_token_exchange(db_engine, async_client):
    """Same catch-all, but for the exchange/userinfo try-block."""
    app = FastAPI()
    _mount(app)
    async_client._transport = ASGITransport(app=app)

    with patch(
        'open_webui.routers.newapi_sso.exchange_code_for_token',
        new=AsyncMock(side_effect=ValueError('boom')),
    ):
        res = await async_client.get('/auth/newapi/callback?code=abc123', follow_redirects=False)

    assert res.status_code == 302
    assert res.headers['location'] == '/auth?error=newapi_sso_failed&reason=server_error'
    assert 'token' not in res.cookies
