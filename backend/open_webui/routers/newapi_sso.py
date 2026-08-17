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
from sqlalchemy.exc import IntegrityError

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

        try:
            user = await _provision_or_login_user(userinfo, db)
            await _store_newapi_token(user.id, token_response['access_token'], token_response['expires_in'], db)

            response = RedirectResponse(url='/', status_code=302)
            await create_session_response(
                request, user, db, response=response, set_cookie=True, source='newapi_sso', expires_delta=NEWAPI_SESSION_TTL
            )
            return response
        except IntegrityError as e:
            # Two concurrent IdP-initiated logins for the same brand-new
            # email (e.g. a double-clicked "Open in Chat" link) can both
            # pass the pre-checks above before either commits, then both
            # attempt to create the user — the loser trips the unique-email
            # constraint here. The race window is tiny, so a retry with a
            # fresh code from new-api almost always succeeds; this is not a
            # routine failure mode, so log it more assertively than the
            # invalid_grant/network_error branches above.
            log.error('new-api SSO provisioning hit a database integrity error (likely a concurrent login race): %s', e)
            return RedirectResponse(url=_reconnect_redirect('server_error'), status_code=302)
