import asyncio
import json
import time

import httpx
import pytest
import respx

from openwebui_rag_mcp_newapi.auth import AuthError, TokenBroker

OWUI = "http://owui.test"
NEWAPI = "http://newapi.test"
TOKEN_PATH = "/oauth2/token-from-key"
NEWAPI_URL = f"{NEWAPI}{TOKEN_PATH}"
EXCHANGE_URL = f"{OWUI}/api/v1/auths/oauth/oidc/token/exchange"


def broker(**kw):
    return TokenBroker(OWUI, NEWAPI, TOKEN_PATH, "oidc", **kw)


def newapi_ok(token="at"):
    return httpx.Response(200, json={"access_token": token, "token_type": "Bearer", "expires_in": 600})


def exchange_ok(jwt="jwt1", expires_at=None):
    return httpx.Response(200, json={"token": jwt, "expires_at": expires_at})


@respx.mock
async def test_fetch_and_exchange():
    n = respx.post(NEWAPI_URL).mock(return_value=newapi_ok())
    e = respx.post(EXCHANGE_URL).mock(return_value=exchange_ok())
    assert await broker().get_jwt("sk-a") == "jwt1"
    assert n.calls[0].request.headers["Authorization"] == "Bearer sk-a"
    assert json.loads(e.calls[0].request.content) == {"token": "at"}


@respx.mock
async def test_cache_reuse():
    n = respx.post(NEWAPI_URL).mock(return_value=newapi_ok())
    e = respx.post(EXCHANGE_URL).mock(return_value=exchange_ok(expires_at=int(time.time()) + 1000))
    b = broker()
    await b.get_jwt("sk-a")
    await b.get_jwt("sk-a")
    assert n.call_count == 1 and e.call_count == 1


@respx.mock
async def test_refresh_before_expiry():
    respx.post(NEWAPI_URL).mock(return_value=newapi_ok())
    e = respx.post(EXCHANGE_URL)
    e.side_effect = [
        exchange_ok("j1", int(time.time()) + 50),
        exchange_ok("j2", int(time.time()) + 1000),
    ]
    b = broker()
    assert await b.get_jwt("sk-a") == "j1"
    assert await b.get_jwt("sk-a") == "j2"
    assert e.call_count == 2


@respx.mock
async def test_null_expires_at_uses_newapi_expires_in(monkeypatch):
    respx.post(NEWAPI_URL).mock(return_value=newapi_ok())
    e = respx.post(EXCHANGE_URL).mock(return_value=exchange_ok(expires_at=None))
    now = [1_000_000.0]
    monkeypatch.setattr("openwebui_rag_mcp_newapi.auth._now", lambda: now[0])
    b = broker()
    await b.get_jwt("sk-a")
    now[0] += 500  # within 600 - 60
    await b.get_jwt("sk-a")
    assert e.call_count == 1
    now[0] += 100  # past 540
    await b.get_jwt("sk-a")
    assert e.call_count == 2


@respx.mock
async def test_concurrent_single_exchange():
    n = respx.post(NEWAPI_URL).mock(return_value=newapi_ok())
    e = respx.post(EXCHANGE_URL).mock(return_value=exchange_ok(expires_at=int(time.time()) + 1000))
    b = broker()
    results = await asyncio.gather(*[b.get_jwt("sk-a") for _ in range(10)])
    assert set(results) == {"jwt1"}
    assert n.call_count == 1 and e.call_count == 1


@pytest.mark.parametrize(
    "status,code,msg",
    [
        (401, "invalid_key", "newapi API 키가 유효하지 않습니다"),
        (403, "user_disabled", "newapi 계정이 비활성화되었습니다"),
        (403, "email_required", "newapi 계정에 이메일이 없습니다. newapi에서 이메일을 등록하세요"),
        (403, "ip_not_allowed", "newapi API 키의 IP 제한에 MCP 서버 IP가 허용되어 있지 않습니다"),
        (403, "feature_disabled", "newapi 관리자가 'API 키로 토큰 발급' 기능을 켜야 합니다"),
        (429, "rate_limited", "잠시 후 다시 시도하세요"),
        (404, "oauth2 is not enabled", "newapi의 OIDC 제공자가 꺼져 있거나 NEWAPI_URL/NEWAPI_TOKEN_PATH가 잘못되었습니다"),
        (403, "unknown", "newapi가 요청을 거부했습니다 (403)"),
        (500, "x", "newapi 인증 서버 오류"),
    ],
)
@respx.mock
async def test_newapi_errors(status, code, msg):
    respx.post(NEWAPI_URL).mock(return_value=httpx.Response(status, json={"error": code, "message": "m"}))
    with pytest.raises(AuthError) as exc:
        await broker().get_jwt("sk-a")
    assert str(exc.value) == msg


@respx.mock
async def test_cache_capped_by_newapi_expires_in(monkeypatch):
    """A long-lived Open WebUI JWT is still re-exchanged when the newapi token expires,
    so a disabled newapi key stops working within its token lifetime."""
    now = [1_000_000.0]
    monkeypatch.setattr("openwebui_rag_mcp_newapi.auth._now", lambda: now[0])
    respx.post(NEWAPI_URL).mock(return_value=newapi_ok())  # expires_in 600
    e = respx.post(EXCHANGE_URL).mock(return_value=exchange_ok(expires_at=now[0] + 28 * 86400))
    b = broker()
    await b.get_jwt("sk-a")
    now[0] += 500
    await b.get_jwt("sk-a")
    assert e.call_count == 1
    now[0] += 100  # past 600 - 60
    await b.get_jwt("sk-a")
    assert e.call_count == 2


@respx.mock
async def test_exchange_no_token():
    respx.post(NEWAPI_URL).mock(return_value=newapi_ok())
    respx.post(EXCHANGE_URL).mock(return_value=httpx.Response(200, json={"next_step": "pending"}))
    b = broker()
    with pytest.raises(AuthError, match="승인 대기 중이거나 MFA가 필요"):
        await b.get_jwt("sk-a")
    assert len(b._cache) == 0


@pytest.mark.parametrize(
    "detail,msg",
    [
        ("Token exchange is disabled", "ENABLE_OAUTH_TOKEN_EXCHANGE를 켜야 합니다"),
        ("User not found", "웹으로 한 번 로그인한 뒤 다시 시도하세요"),
        ("ACCESS_PROHIBITED", "허용되지 않은 클라이언트이거나 이메일 도메인입니다"),
    ],
)
@respx.mock
async def test_exchange_403_variants(detail, msg):
    respx.post(NEWAPI_URL).mock(return_value=newapi_ok())
    respx.post(EXCHANGE_URL).mock(return_value=httpx.Response(403, json={"detail": detail}))
    with pytest.raises(AuthError, match=msg):
        await broker().get_jwt("sk-a")


@pytest.mark.parametrize(
    "status,msg",
    [
        (400, "newapi 토큰이 Open WebUI에서 거부되었습니다"),
        (404, "OPENWEBUI_OAUTH_PROVIDER 값을 확인하세요"),
        (429, "Open WebUI 토큰 교환 호출 제한에 걸렸습니다"),
    ],
)
@respx.mock
async def test_exchange_400_404_429(status, msg):
    respx.post(NEWAPI_URL).mock(return_value=newapi_ok())
    respx.post(EXCHANGE_URL).mock(return_value=httpx.Response(status, json={"detail": "x"}))
    with pytest.raises(AuthError, match=msg):
        await broker().get_jwt("sk-a")


@respx.mock
async def test_no_secret_leak(caplog):
    caplog.set_level("DEBUG")
    respx.post(NEWAPI_URL).mock(return_value=newapi_ok("secret-access-token"))
    respx.post(EXCHANGE_URL).mock(return_value=exchange_ok("secret-jwt"))
    b = broker()
    await b.get_jwt("sk-supersecret")
    for k in b._cache:
        assert "sk-supersecret" not in k
    for text in (caplog.text, repr(b), repr(b.for_key("sk-supersecret"))):
        for s in ("sk-supersecret", "secret-access-token", "secret-jwt"):
            assert s not in text
    # error path
    respx.post(NEWAPI_URL).mock(return_value=httpx.Response(401))
    with pytest.raises(AuthError) as exc:
        await broker().get_jwt("sk-supersecret")
    assert "sk-supersecret" not in str(exc.value)


@respx.mock
async def test_eviction_max_entries():
    n = respx.post(NEWAPI_URL).mock(return_value=newapi_ok())
    respx.post(EXCHANGE_URL).mock(return_value=exchange_ok(expires_at=int(time.time()) + 1000))
    b = broker(max_entries=2)
    for k in ("sk-1", "sk-2", "sk-3"):
        await b.get_jwt(k)
    assert len(b._cache) == 2
    assert len(b._locks) == 0
    assert n.call_count == 3
    await b.get_jwt("sk-3")  # still cached
    assert n.call_count == 3
    await b.get_jwt("sk-1")  # evicted -> refetch
    assert n.call_count == 4


@respx.mock
async def test_invalidate_and_provider():
    n = respx.post(NEWAPI_URL).mock(return_value=newapi_ok())
    respx.post(EXCHANGE_URL).mock(return_value=exchange_ok(expires_at=int(time.time()) + 1000))
    p = broker().for_key("sk-a")
    await p.get_token()
    await p.get_token()
    assert n.call_count == 1
    await p.invalidate()
    await p.get_token()
    assert n.call_count == 2


@respx.mock
async def test_concurrent_invalid_key_cleans_locks():
    respx.post(NEWAPI_URL).mock(return_value=httpx.Response(401))
    b = broker()
    results = await asyncio.gather(*[b.get_jwt("sk-bad") for _ in range(10)], return_exceptions=True)
    assert all(isinstance(r, AuthError) for r in results)
    assert b._locks == {}


@respx.mock
async def test_many_failing_keys_leave_no_locks():
    respx.post(NEWAPI_URL).mock(return_value=httpx.Response(401))
    b = broker()
    for i in range(50):
        with pytest.raises(AuthError):
            await b.get_jwt(f"sk-bad-{i}")
    assert b._locks == {}


@respx.mock
async def test_non_numeric_expires_at_falls_back():
    respx.post(NEWAPI_URL).mock(return_value=newapi_ok())
    e = respx.post(EXCHANGE_URL).mock(return_value=exchange_ok(expires_at="never"))
    b = broker()
    await b.get_jwt("sk-a")
    await b.get_jwt("sk-a")
    assert e.call_count == 1
