import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest


class _FakeResponse:
    def __init__(self, status, payload=None, json_error=None):
        self.status = status
        self._payload = payload
        self._json_error = json_error

    async def json(self, **kwargs):
        if self._json_error is not None:
            raise self._json_error
        return self._payload


class _FakeSession:
    def __init__(self, response):
        self._response = response

    def post(self, *args, **kwargs):
        return _CtxManager(self._response)

    def get(self, *args, **kwargs):
        return _CtxManager(self._response)


class _CtxManager:
    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self._response

    async def __aexit__(self, *exc):
        return False


@pytest.mark.asyncio
async def test_exchange_code_for_token_success():
    from open_webui.utils.newapi_oauth import exchange_code_for_token

    fake_session = _FakeSession(
        _FakeResponse(200, {'access_token': 'sk-abc', 'token_type': 'Bearer', 'expires_in': 86400})
    )
    with patch('open_webui.utils.newapi_oauth.get_session', new=AsyncMock(return_value=fake_session)):
        result = await exchange_code_for_token('a-code')

    assert result == {'access_token': 'sk-abc', 'expires_in': 86400}


@pytest.mark.asyncio
async def test_exchange_code_for_token_invalid_grant():
    from open_webui.utils.newapi_oauth import NewapiOAuthError, exchange_code_for_token

    fake_session = _FakeSession(_FakeResponse(400, {'error': 'invalid_grant'}))
    with patch('open_webui.utils.newapi_oauth.get_session', new=AsyncMock(return_value=fake_session)):
        with pytest.raises(NewapiOAuthError) as exc_info:
            await exchange_code_for_token('a-code')

    assert exc_info.value.reason == 'invalid_grant'


@pytest.mark.asyncio
async def test_exchange_code_for_token_invalid_client():
    from open_webui.utils.newapi_oauth import NewapiOAuthError, exchange_code_for_token

    fake_session = _FakeSession(_FakeResponse(401, {'error': 'invalid_client'}))
    with patch('open_webui.utils.newapi_oauth.get_session', new=AsyncMock(return_value=fake_session)):
        with pytest.raises(NewapiOAuthError) as exc_info:
            await exchange_code_for_token('a-code')

    assert exc_info.value.reason == 'invalid_client'


@pytest.mark.asyncio
async def test_exchange_code_for_token_network_error():
    from open_webui.utils.newapi_oauth import NewapiOAuthError, exchange_code_for_token

    async def _raise_session():
        import aiohttp

        raise aiohttp.ClientConnectionError('boom')

    with patch('open_webui.utils.newapi_oauth.get_session', new=_raise_session):
        with pytest.raises(NewapiOAuthError) as exc_info:
            await exchange_code_for_token('a-code')

    assert exc_info.value.reason == 'network_error'


@pytest.mark.asyncio
async def test_fetch_userinfo_success_defaults_is_admin_false():
    from open_webui.utils.newapi_oauth import fetch_userinfo

    fake_session = _FakeSession(_FakeResponse(200, {'sub': 'u1', 'email': 'a@b.com', 'name': 'A'}))
    with patch('open_webui.utils.newapi_oauth.get_session', new=AsyncMock(return_value=fake_session)):
        result = await fetch_userinfo('sk-abc')

    assert result == {'sub': 'u1', 'email': 'a@b.com', 'name': 'A', 'is_admin': False}


@pytest.mark.asyncio
async def test_fetch_userinfo_missing_sub_raises():
    from open_webui.utils.newapi_oauth import NewapiOAuthError, fetch_userinfo

    fake_session = _FakeSession(_FakeResponse(200, {'email': 'a@b.com', 'name': 'A'}))
    with patch('open_webui.utils.newapi_oauth.get_session', new=AsyncMock(return_value=fake_session)):
        with pytest.raises(NewapiOAuthError) as exc_info:
            await fetch_userinfo('sk-abc')

    assert exc_info.value.reason == 'invalid_userinfo'


class _FakeSessionRaisesOnCall:
    """A session whose post()/get() raise synchronously, before any `async with`."""

    def __init__(self, exc):
        self._exc = exc

    def post(self, *args, **kwargs):
        raise self._exc

    def get(self, *args, **kwargs):
        raise self._exc


@pytest.mark.asyncio
async def test_exchange_code_for_token_total_timeout_raises_network_error():
    from open_webui.utils.newapi_oauth import NewapiOAuthError, exchange_code_for_token

    # A bare asyncio.TimeoutError (not aiohttp.ClientError) is exactly what
    # aiohttp raises when a session-level *total* timeout fires.
    fake_session = _FakeSessionRaisesOnCall(asyncio.TimeoutError())
    with patch('open_webui.utils.newapi_oauth.get_session', new=AsyncMock(return_value=fake_session)):
        with pytest.raises(NewapiOAuthError) as exc_info:
            await exchange_code_for_token('a-code')

    assert exc_info.value.reason == 'network_error'


@pytest.mark.asyncio
async def test_exchange_code_for_token_malformed_json_raises_network_error():
    from open_webui.utils.newapi_oauth import NewapiOAuthError, exchange_code_for_token

    fake_session = _FakeSession(_FakeResponse(200, json_error=json.JSONDecodeError('bad json', 'doc', 0)))
    with patch('open_webui.utils.newapi_oauth.get_session', new=AsyncMock(return_value=fake_session)):
        with pytest.raises(NewapiOAuthError) as exc_info:
            await exchange_code_for_token('a-code')

    assert exc_info.value.reason == 'network_error'


@pytest.mark.asyncio
async def test_exchange_code_for_token_missing_access_token_raises_network_error():
    from open_webui.utils.newapi_oauth import NewapiOAuthError, exchange_code_for_token

    fake_session = _FakeSession(_FakeResponse(200, {'expires_in': 86400}))
    with patch('open_webui.utils.newapi_oauth.get_session', new=AsyncMock(return_value=fake_session)):
        with pytest.raises(NewapiOAuthError) as exc_info:
            await exchange_code_for_token('a-code')

    assert exc_info.value.reason == 'network_error'


@pytest.mark.asyncio
async def test_fetch_userinfo_total_timeout_raises_network_error():
    from open_webui.utils.newapi_oauth import NewapiOAuthError, fetch_userinfo

    fake_session = _FakeSessionRaisesOnCall(asyncio.TimeoutError())
    with patch('open_webui.utils.newapi_oauth.get_session', new=AsyncMock(return_value=fake_session)):
        with pytest.raises(NewapiOAuthError) as exc_info:
            await fetch_userinfo('sk-abc')

    assert exc_info.value.reason == 'network_error'


@pytest.mark.asyncio
async def test_fetch_userinfo_malformed_json_raises_network_error():
    from open_webui.utils.newapi_oauth import NewapiOAuthError, fetch_userinfo

    fake_session = _FakeSession(_FakeResponse(200, json_error=json.JSONDecodeError('bad json', 'doc', 0)))
    with patch('open_webui.utils.newapi_oauth.get_session', new=AsyncMock(return_value=fake_session)):
        with pytest.raises(NewapiOAuthError) as exc_info:
            await fetch_userinfo('sk-abc')

    assert exc_info.value.reason == 'network_error'
