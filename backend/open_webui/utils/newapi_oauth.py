"""
Server-to-server client for new-api's OAuth2 provider.

Required deployment env vars (set to '' / False by default, meaning SSO is
off until configured):
    ENABLE_NEWAPI_SSO=True
    NEWAPI_OAUTH_BASE_URL=https://<newapi-host>
    NEWAPI_OAUTH_CLIENT_ID=<provided by new-api ops>
    NEWAPI_OAUTH_CLIENT_SECRET=<provided by new-api ops>
    NEWAPI_ENTRY_URL=<URL shown to users to re-enter new-api>

See docs/superpowers/specs/2026-08-17-newapi-oauth2-sso-integration-design.md
and docs/2026-08-17-openwebui-response-to-newapi-oauth2-handoff.md for the
full protocol contract this module implements against.
"""

import asyncio
import logging

import aiohttp
from open_webui.env import NEWAPI_OAUTH_BASE_URL, NEWAPI_OAUTH_CLIENT_ID, NEWAPI_OAUTH_CLIENT_SECRET
from open_webui.utils.session_pool import get_session

log = logging.getLogger(__name__)


class NewapiOAuthError(Exception):
    def __init__(self, reason: str, message: str | None = None):
        self.reason = reason
        super().__init__(message or reason)


async def exchange_code_for_token(code: str) -> dict:
    """POST /oauth2/token. Returns {'access_token': str, 'expires_in': int}."""
    try:
        session = await get_session()
        async with session.post(
            f'{NEWAPI_OAUTH_BASE_URL}/oauth2/token',
            data={
                'grant_type': 'authorization_code',
                'code': code,
                'client_id': NEWAPI_OAUTH_CLIENT_ID,
                'client_secret': NEWAPI_OAUTH_CLIENT_SECRET,
            },
        ) as response:
            payload = await response.json()
            if response.status == 400 and payload.get('error') == 'invalid_grant':
                raise NewapiOAuthError('invalid_grant', 'new-api rejected the authorization code')
            if response.status == 401 and payload.get('error') == 'invalid_client':
                log.error('new-api token exchange failed: invalid_client (check NEWAPI_OAUTH_CLIENT_ID/SECRET)')
                raise NewapiOAuthError('invalid_client', 'OpenWebUI is misconfigured for new-api SSO')
            if response.status != 200:
                log.error('Unexpected new-api token exchange response: %s %s', response.status, payload)
                raise NewapiOAuthError('network_error', f'Unexpected response status {response.status}')
            access_token = payload.get('access_token')
            expires_in = payload.get('expires_in')
            if not access_token or expires_in is None:
                log.error("new-api token exchange response missing 'access_token' or 'expires_in': %s", payload)
                raise NewapiOAuthError('network_error', "Response missing 'access_token' or 'expires_in'")
            return {'access_token': access_token, 'expires_in': expires_in}
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
        log.error('Network error exchanging new-api authorization code: %s', e)
        raise NewapiOAuthError('network_error', str(e)) from e


async def fetch_userinfo(access_token: str) -> dict:
    """GET /oauth2/userinfo. Returns {'sub', 'email', 'name', 'is_admin'}.

    `email` is optional on new-api's side (not required at signup, and
    mutable afterward) -- only `sub` is treated as required here, since
    that's the durable identity new-api guarantees. Account matching in
    newapi_sso.py's _provision_or_login_user already keys off `sub`, not
    email; `email` may come back as '' or None and callers must handle
    that (see _provision_or_login_user's placeholder-email fallback).
    """
    try:
        session = await get_session()
        async with session.get(
            f'{NEWAPI_OAUTH_BASE_URL}/oauth2/userinfo',
            headers={'Authorization': f'Bearer {access_token}'},
        ) as response:
            payload = await response.json()
            if response.status != 200:
                log.error('Unexpected new-api userinfo response: %s %s', response.status, payload)
                raise NewapiOAuthError('network_error', f'Unexpected response status {response.status}')
            sub = payload.get('sub')
            if not sub:
                log.error("new-api userinfo response missing 'sub': %s", payload)
                raise NewapiOAuthError('invalid_userinfo', "Response missing 'sub'")
            email = payload.get('email') or ''
            return {
                'sub': sub,
                'email': email,
                'name': payload.get('name') or email or f'new-api user {sub}',
                'is_admin': bool(payload.get('is_admin', False)),
            }
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
        log.error('Network error fetching new-api userinfo: %s', e)
        raise NewapiOAuthError('network_error', str(e)) from e
