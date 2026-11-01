"""Exchange a newapi API key for an Open WebUI JWT, with a per-key cache."""

import asyncio
import hashlib
import time
from collections import OrderedDict

import httpx

from .client import OpenWebUIError

REFRESH_MARGIN = 60.0
DEFAULT_TTL = 3600.0

_NEWAPI_ERRORS = {
    "invalid_key": "newapi API 키가 유효하지 않습니다",
    "user_disabled": "newapi 계정이 비활성화되었습니다",
    "email_required": "newapi 계정에 이메일이 없습니다. newapi에서 이메일을 등록하세요",
    "ip_not_allowed": "newapi API 키의 IP 제한에 MCP 서버 IP가 허용되어 있지 않습니다",
    "feature_disabled": "newapi 관리자가 'API 키로 토큰 발급' 기능을 켜야 합니다",
    "rate_limited": "잠시 후 다시 시도하세요",
}
_NEWAPI_BY_STATUS = {
    401: _NEWAPI_ERRORS["invalid_key"],
    404: "newapi의 OIDC 제공자가 꺼져 있거나 NEWAPI_URL/NEWAPI_TOKEN_PATH가 잘못되었습니다",
    429: _NEWAPI_ERRORS["rate_limited"],
}
_NEWAPI_OTHER = "newapi 인증 서버 오류"
_NO_TOKEN = (
    "Open WebUI 계정이 승인 대기 중이거나 MFA가 필요해 외부 인증을 할 수 없습니다. 관리자에게 문의하세요"
)


def _now() -> float:
    return time.time()


class AuthError(OpenWebUIError):
    """Raised when a newapi key cannot be turned into an Open WebUI JWT."""


def _detail(resp: httpx.Response) -> str:
    try:
        d = resp.json().get("detail", "")
    except (ValueError, AttributeError):
        return ""
    return d if isinstance(d, str) else str(d)


def _newapi_error(resp: httpx.Response) -> AuthError:
    try:
        code = resp.json().get("error")
    except (ValueError, AttributeError):
        code = None
    if code in _NEWAPI_ERRORS:
        return AuthError(_NEWAPI_ERRORS[code])
    if resp.status_code in _NEWAPI_BY_STATUS:
        return AuthError(_NEWAPI_BY_STATUS[resp.status_code])
    if resp.status_code == 403:
        return AuthError("newapi가 요청을 거부했습니다 (403)")
    return AuthError(_NEWAPI_OTHER)


def _exchange_error(resp: httpx.Response) -> AuthError:
    code = resp.status_code
    if code == 403:
        d = _detail(resp)
        if "disabled" in d.lower():
            return AuthError("Open WebUI 관리자가 ENABLE_OAUTH_TOKEN_EXCHANGE를 켜야 합니다")
        if "user not found" in d.lower():
            return AuthError("Open WebUI에 웹으로 한 번 로그인한 뒤 다시 시도하세요")
        if "ACCESS_PROHIBITED" in d:
            return AuthError(
                "허용되지 않은 클라이언트이거나 이메일 도메인입니다 "
                "(OAUTH_TOKEN_EXCHANGE_TRUSTED_CLIENT_IDS / 허용 도메인 확인)"
            )
        return AuthError("Open WebUI 토큰 교환이 거부되었습니다 (403)")
    if code == 400:
        return AuthError(
            "newapi 토큰이 Open WebUI에서 거부되었습니다 "
            "(sub/email 누락 또는 검증 실패). newapi 설정을 확인하세요"
        )
    if code == 404:
        return AuthError("OPENWEBUI_OAUTH_PROVIDER 값을 확인하세요")
    if code == 429:
        return AuthError("Open WebUI 토큰 교환 호출 제한에 걸렸습니다. 잠시 후 다시 시도하세요")
    return AuthError(f"Open WebUI 토큰 교환 오류 (HTTP {code})")


class TokenProvider:
    """Adapter binding a TokenBroker to one API key, for OpenWebUIClient."""

    def __init__(self, broker: "TokenBroker", api_key: str):
        self._broker = broker
        self._api_key = api_key

    async def get_token(self) -> str:
        return await self._broker.get_jwt(self._api_key)

    async def invalidate(self) -> None:
        await self._broker.invalidate(self._api_key)

    def __repr__(self) -> str:
        return "TokenProvider(<redacted>)"


class TokenBroker:
    def __init__(
        self,
        openwebui_url: str,
        newapi_url: str,
        newapi_token_path: str,
        provider: str,
        timeout: float = 30.0,
        max_entries: int = 1000,
    ):
        self._exchange_url = (
            f"{openwebui_url.rstrip('/')}/api/v1/auths/oauth/{provider}/token/exchange"
        )
        self._newapi_url = f"{newapi_url.rstrip('/')}{newapi_token_path}"
        self._timeout = timeout
        self._max = max_entries
        self._cache: OrderedDict[str, tuple[str, float]] = OrderedDict()
        self._locks: dict[str, list] = {}  # key -> [Lock, refcount]

    def __repr__(self) -> str:
        return f"TokenBroker(entries={len(self._cache)})"

    @staticmethod
    def _key(api_key: str) -> str:
        return hashlib.sha256(api_key.encode()).hexdigest()

    def for_key(self, api_key: str) -> TokenProvider:
        return TokenProvider(self, api_key)

    def _cached(self, k: str) -> str | None:
        entry = self._cache.get(k)
        if entry and entry[1] - REFRESH_MARGIN > _now():
            self._cache.move_to_end(k)
            return entry[0]
        return None

    async def get_jwt(self, api_key: str) -> str:
        k = self._key(api_key)
        if (jwt := self._cached(k)) is not None:
            return jwt
        entry = self._locks.setdefault(k, [asyncio.Lock(), 0])
        entry[1] += 1
        try:
            async with entry[0]:
                if (jwt := self._cached(k)) is not None:
                    return jwt
                jwt, expiry = await self._fetch(api_key)
                self._cache[k] = (jwt, expiry)
                self._cache.move_to_end(k)
                self._evict()
                return jwt
        finally:
            entry[1] -= 1
            if entry[1] == 0 and self._locks.get(k) is entry:
                del self._locks[k]

    def _evict(self) -> None:
        while len(self._cache) > self._max:
            self._cache.popitem(last=False)

    async def invalidate(self, api_key: str) -> None:
        self._cache.pop(self._key(api_key), None)

    async def _post(self, url: str, **kwargs) -> httpx.Response:
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as http:
                return await http.post(url, **kwargs)
        except httpx.TimeoutException:
            raise AuthError("인증 서버 요청 시간이 초과되었습니다") from None
        except httpx.HTTPError:
            raise AuthError("인증 서버에 연결할 수 없습니다") from None

    async def _fetch(self, api_key: str) -> tuple[str, float]:
        resp = await self._post(self._newapi_url, headers={"Authorization": f"Bearer {api_key}"})
        if resp.status_code != 200:
            raise _newapi_error(resp)
        try:
            body = resp.json()
            access_token = body["access_token"]
        except (ValueError, KeyError, TypeError):
            raise AuthError(_NEWAPI_OTHER) from None
        # Never cache longer than the newapi token lives, so a disabled newapi
        # key or user stops working within that lifetime.
        try:
            newapi_expiry = _now() + float(body.get("expires_in") or DEFAULT_TTL)
        except (TypeError, ValueError):
            newapi_expiry = _now() + DEFAULT_TTL

        resp = await self._post(self._exchange_url, json={"token": access_token})
        if resp.status_code != 200:
            raise _exchange_error(resp)
        try:
            data = resp.json()
        except ValueError:
            raise AuthError("Open WebUI 토큰 교환 응답을 해석할 수 없습니다") from None
        jwt = data.get("token") if isinstance(data, dict) else None
        if not jwt:
            raise AuthError(_NO_TOKEN)
        try:
            expiry = float(data["expires_at"])
        except (KeyError, TypeError, ValueError):
            expiry = newapi_expiry
        return jwt, min(expiry, newapi_expiry)
