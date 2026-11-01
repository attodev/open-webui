#!/usr/bin/env bash
# new-api 컨테이너가 뜬 뒤 한 번 실행한다. 여러 번 실행해도 안전하다(멱등).
#
#   - 초기 설정(루트 계정) 수행
#   - 서비스 계정 + 서비스 토큰 생성 → 토큰 키를 .env 의 OWUI_SERVICE_KEY 에 기록
#   - relay(openwebui.*)·OIDC(oauth2.*) 옵션 주입, ServerAddress 를 http://PUBLIC_HOST:PORT 로 설정
#   - NEW_API_TEST_USERS 의 테스트 사용자 생성 + 이메일 설정(psql)
#   - MOCK_UPSTREAM=1 이면 mock-gpt 채널과 가격, 테스트 사용자 쿼터까지 넣는다
#
# 요구: bash, python3(표준 라이브러리만), docker compose (테스트 사용자 이메일/쿼터는 psql 로 넣는다)
set -euo pipefail
cd "$(dirname "$0")"
if [ ! -f .env ]; then
  echo ".env 가 없습니다. cp .env.example .env 후 값을 채우세요." >&2
  exit 1
fi
set -a
# shellcheck disable=SC1091
. ./.env
set +a
export NEW_API_URL="${NEW_API_URL:-http://localhost:${NEW_API_PORT:-3000}}"

python3 - <<'PY'
import json, os, subprocess, sys, time, urllib.request, urllib.error
from http.cookiejar import CookieJar

B = os.environ["NEW_API_URL"].rstrip("/")
E = os.environ
PUBLIC_BASE = f"http://{E['PUBLIC_HOST']}:{E.get('NEW_API_PORT','3000')}"
OWUI_BASE = f"http://{E['PUBLIC_HOST']}:{E.get('OPEN_WEBUI_PORT','8080')}"

def log(msg): print(f"[bootstrap] {msg}", flush=True)
def die(msg): print(f"[bootstrap] 실패: {msg}", file=sys.stderr); sys.exit(1)

class Session:
    def __init__(self):
        self.jar = CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        self.user_id = None
    def call(self, method, path, body=None, raw=False):
        data = None; headers = {}
        if body is not None:
            data = json.dumps(body).encode(); headers["Content-Type"] = "application/json"
        if self.user_id is not None:
            headers["New-Api-User"] = str(self.user_id)
        req = urllib.request.Request(B + path, data=data, method=method, headers=headers)
        try:
            with self.opener.open(req, timeout=20) as r:
                text = r.read().decode(); status = r.status
        except urllib.error.HTTPError as e:
            text = e.read().decode(); status = e.code
        if raw: return status, text
        try: return status, json.loads(text)
        except json.JSONDecodeError: return status, {"success": False, "message": text[:200]}
    def login(self, user, pw):
        st, j = self.call("POST", "/api/user/login", {"username": user, "password": pw})
        if not j.get("success"): return False
        self.user_id = j["data"]["id"]; return True

# 1. 기동 대기
for i in range(60):
    try:
        st, j = Session().call("GET", "/api/status")
        if j.get("success"): break
    except Exception: pass
    time.sleep(2)
else:
    die(f"{B} 가 응답하지 않습니다")
log(f"new-api 응답 확인: {B}")

# 2. 초기 설정
st, setup = Session().call("GET", "/api/setup")
if not setup["data"]["status"]:
    st, j = Session().call("POST", "/api/setup", {
        "username": E["NEW_API_ROOT_USER"], "password": E["NEW_API_ROOT_PASSWORD"],
        "confirmPassword": E["NEW_API_ROOT_PASSWORD"], "SelfUseModeEnabled": False, "DemoSiteEnabled": False})
    if not j.get("success"): die(f"setup: {j.get('message')}")
    log("초기 설정 완료 (루트 생성)")
else:
    log("초기 설정은 이미 되어 있음")

root = Session()
if not root.login(E["NEW_API_ROOT_USER"], E["NEW_API_ROOT_PASSWORD"]): die("루트 로그인 실패 (.env 의 NEW_API_ROOT_PASSWORD 확인)")

def ensure_user(username, password, display):
    s = Session()
    if s.login(username, password): return s
    st, j = root.call("POST", "/api/user/", {"username": username, "password": password, "display_name": display})
    if not j.get("success"): die(f"사용자 {username} 생성: {j.get('message')}")
    if not s.login(username, password): die(f"{username} 로그인 실패")
    log(f"사용자 생성: {username}")
    return s

# 3. 서비스 계정 + 토큰
svc = ensure_user(E["NEW_API_SERVICE_USER"], E["NEW_API_SERVICE_PASSWORD"], "Open WebUI service")
st, j = svc.call("GET", "/api/token/?p=1&page_size=50")
items = j["data"].get("items", j["data"]) if isinstance(j["data"], dict) else j["data"]
tok = next((t for t in items if t["name"] == "openwebui-service"), None)
if tok is None:
    st, j = svc.call("POST", "/api/token/", {"name": "openwebui-service", "unlimited_quota": True, "expired_time": -1, "remain_quota": 0})
    if not j.get("success"): die(f"서비스 토큰 생성: {j.get('message')}")
    st, j = svc.call("GET", "/api/token/?p=1&page_size=50")
    items = j["data"].get("items", j["data"]) if isinstance(j["data"], dict) else j["data"]
    tok = next(t for t in items if t["name"] == "openwebui-service")
    log("서비스 토큰 생성: openwebui-service (쿼터 무제한, 만료 없음)")
st, j = svc.call("POST", f"/api/token/{tok['id']}/key")
key = j["data"] if isinstance(j["data"], str) else j["data"]["key"]

# .env 에 기록
lines = open(".env").read().splitlines()
out, seen = [], False
for ln in lines:
    if ln.startswith("OWUI_SERVICE_KEY="): out.append(f"OWUI_SERVICE_KEY={key}"); seen = True
    else: out.append(ln)
if not seen: out.append(f"OWUI_SERVICE_KEY={key}")
open(".env", "w").write("\n".join(out) + "\n")
log("OWUI_SERVICE_KEY 를 .env 에 기록")

# 4. 옵션
def opt(k, v):
    st, j = root.call("PUT", "/api/option/", {"key": k, "value": v})
    if not j.get("success"): die(f"옵션 {k}: {j.get('message')}")
opt("ServerAddress", PUBLIC_BASE)
opt("openwebui.service_api_key", key)
opt("openwebui.jwt_secret", E["OWUI_JWT_SECRET"])
opt("openwebui.jwt_issuer", "open-webui")
opt("openwebui.allow_unsigned_requests", True)
opt("openwebui.relay_enabled", True)
opt("oauth2.client_id", E.get("OIDC_CLIENT_ID", "openwebui"))
opt("oauth2.client_secret", E["OIDC_CLIENT_SECRET"])
opt("oauth2.redirect_uri", OWUI_BASE + "/oauth/oidc/login/callback")
opt("oauth2.app_url", OWUI_BASE)
opt("oauth2.post_logout_redirect_uri", OWUI_BASE + "/auth")
opt("oauth2.enabled", True)
opt("oauth2.token_from_key_client_id", E.get("OIDC_MCP_CLIENT_ID", "openwebui-mcp"))
opt("oauth2.token_from_key_enabled", True)
log(f"옵션 주입 완료 (issuer={PUBLIC_BASE}, redirect_uri={OWUI_BASE}/oauth/oidc/login/callback)")

def psql(sql):
    cmd = ["docker", "compose", "exec", "-T", "postgres", "psql", "-U", E.get("PG_USER", "newapi"), "-d", E.get("PG_DB", "newapi"), "-v", "ON_ERROR_STOP=1", "-c", sql]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0: die(f"psql: {r.stderr.strip()}")

# 5. 루트 이메일 (선택) + 테스트 사용자 (관리자 생성 API 는 이메일을 저장하지 않으므로 psql 로 넣는다)
if E.get("NEW_API_ROOT_EMAIL"):
    psql(f"update users set email = '{E['NEW_API_ROOT_EMAIL']}' where username = '{E['NEW_API_ROOT_USER']}';")
    log(f"루트 이메일 설정: {E['NEW_API_ROOT_EMAIL']}")
test_users = [u for u in E.get("NEW_API_TEST_USERS", "").split(",") if u.strip()]
for spec in test_users:
    name, pw, email = (spec.split(":") + ["", ""])[:3]
    ensure_user(name, pw, name.capitalize())
    if email:
        psql(f"update users set email = '{email}' where username = '{name}';")
        log(f"{name} 이메일 설정: {email}")

# 6. mock 업스트림
if E.get("MOCK_UPSTREAM", "0") == "1":
    st, j = root.call("GET", "/api/channel/?p=1&page_size=50")
    chans = j["data"].get("items", j["data"]) if isinstance(j["data"], dict) else j["data"]
    if not any(c.get("name") == "mock-upstream" for c in chans or []):
        st, j = root.call("POST", "/api/channel/", {"mode": "single", "channel": {
            "type": 1, "name": "mock-upstream", "key": "mock-key", "base_url": "http://mock-upstream:9100",
            "models": "mock-gpt", "group": "default", "status": 1, "priority": 0, "weight": 0}})
        if not j.get("success"): die(f"mock 채널: {j.get('message')}")
        log("mock-upstream 채널 생성 (모델 mock-gpt)")
    st, j = root.call("GET", "/api/option/")
    opts = {o["key"]: o["value"] for o in j["data"]}
    for k in ("ModelRatio", "CompletionRatio"):
        m = json.loads(opts.get(k) or "{}"); m["mock-gpt"] = 1; opt(k, json.dumps(m))
    for spec in test_users:
        name = spec.split(":")[0]
        psql(f"update users set quota = 5000000 where username = '{name}';")
    log("mock-gpt 가격(비율 1)과 테스트 사용자 쿼터 설정")

log("완료. 다음: docker compose up -d   (Open WebUI 가 .env 의 OWUI_SERVICE_KEY 로 기동)")
log(f"Open WebUI: {OWUI_BASE}   new-api: {PUBLIC_BASE}")
PY
