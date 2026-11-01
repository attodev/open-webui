#!/usr/bin/env bash
# new-api ↔ Open WebUI ↔ RAG MCP 를 서로 연동된 상태로 띄운다. 여러 번 실행해도 안전하다(멱등).
#
#   1. 필요한 이미지가 로컬에 있는지 확인 (빌드·다운로드는 하지 않는다)
#   2. .env 의 빈 비밀값·비밀번호를 무작위로 생성해 .env 에 기록
#   3. PostgreSQL·Redis·new-api 기동
#   4. new-api 설정: 루트(관리자) 계정, Open WebUI 서비스 계정·토큰, OIDC 제공자(Open WebUI 로그인),
#      relay JWT, API 키 → OIDC 토큰 발급(MCP), ServerAddress. 서비스 토큰 키를 .env 의 OWUI_SERVICE_KEY 에 기록
#   5. Open WebUI·MCP 서버 기동 (.env 값으로)
#   6. 연동 확인: Open WebUI → new-api discovery, OIDC 로그인 리다이렉트, 토큰 교환, MCP → new-api·Open WebUI
#
# 모델·임베딩 채널, 사용자, 지식 베이스는 만들지 않는다. 그 다음 할 일은 README 5절.
#
# 요구: bash, python3(표준 라이브러리만), docker compose v2. 이 호스트에서 http://localhost:NEW_API_PORT 로 new-api 에 닿아야 한다.
set -euo pipefail
cd "$(dirname "$0")"
if [ ! -f .env ]; then
  echo ".env 가 없습니다. cp .env.example .env 후 PUBLIC_HOST 와 이미지 태그를 확인하세요." >&2
  exit 1
fi

exec python3 - "$@" <<'PY'
import json, os, re, secrets, string, subprocess, sys, time
import urllib.request, urllib.error
from http.cookiejar import CookieJar

ENV_FILE = ".env"

def log(msg): print(f"[bootstrap] {msg}", flush=True)
def die(msg): print(f"[bootstrap] 실패: {msg}", file=sys.stderr, flush=True); sys.exit(1)

# ---------- .env ----------
def read_env():
    env = {}
    for ln in open(ENV_FILE).read().splitlines():
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", ln)
        if m: env[m.group(1)] = m.group(2)
    return env

def write_env(updates):
    lines = open(ENV_FILE).read().splitlines()
    seen = set(); out = []
    for ln in lines:
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=", ln)
        if m and m.group(1) in updates:
            out.append(f"{m.group(1)}={updates[m.group(1)]}"); seen.add(m.group(1))
        else:
            out.append(ln)
    out += [f"{k}={v}" for k, v in updates.items() if k not in seen]
    open(ENV_FILE, "w").write("\n".join(out) + "\n")

def token(n=32): return secrets.token_hex(n)
def password(n=16):
    alphabet = string.ascii_letters + string.digits
    while True:
        p = "".join(secrets.choice(alphabet) for _ in range(n))
        if any(c.isdigit() for c in p) and any(c.isalpha() for c in p): return p

GENERATED = {
    "PG_PASSWORD": lambda: token(16), "REDIS_PASSWORD": lambda: token(16),
    "NEW_API_SESSION_SECRET": token, "NEW_API_CRYPTO_SECRET": token,
    "OIDC_CLIENT_SECRET": lambda: token(24), "OWUI_JWT_SECRET": token, "OWUI_SECRET_KEY": token,
    "NEW_API_ROOT_PASSWORD": password, "NEW_API_SERVICE_PASSWORD": password,
}

E = read_env()
if not E.get("PUBLIC_HOST") or "localhost" in E["PUBLIC_HOST"] or E["PUBLIC_HOST"].startswith("127."):
    die("PUBLIC_HOST 를 브라우저와 컨테이너가 모두 닿는 이름으로 설정하세요 (localhost 불가, README 2절)")
for k in ("NEW_API_ROOT_PASSWORD", "NEW_API_SERVICE_PASSWORD"):
    if E.get(k) and not 8 <= len(E[k]) <= 20: die(f"{k} 는 8~20자여야 합니다 (new-api 규칙)")
new = {k: f() for k, f in GENERATED.items() if not E.get(k)}
if new:
    write_env(new); E.update(new)
    log("빈 값 생성: " + ", ".join(new))
generated_root_pw = "NEW_API_ROOT_PASSWORD" in new

PUBLIC_NEWAPI = f"http://{E['PUBLIC_HOST']}:{E.get('NEW_API_PORT', '3000')}"
PUBLIC_OWUI = f"http://{E['PUBLIC_HOST']}:{E.get('OPEN_WEBUI_PORT', '8080')}"
LOCAL_NEWAPI = f"http://localhost:{E.get('NEW_API_PORT', '3000')}"
LOCAL_OWUI = f"http://localhost:{E.get('OPEN_WEBUI_PORT', '8080')}"
PROFILES = ["--profile", "owui-key"] if E.get("OWUI_ENABLE_API_KEYS", "false") == "true" else []

def compose(*args, check=True, capture=False):
    r = subprocess.run(["docker", "compose", *PROFILES, *args], capture_output=capture, text=True)
    if check and r.returncode != 0:
        die(f"docker compose {' '.join(args)} 실패" + (f": {r.stderr.strip()[-500:]}" if capture else ""))
    return r

# ---------- 1. 이미지 ----------
images = [E.get("NEW_API_IMAGE", "new-api-owui:latest"), E.get("OPEN_WEBUI_IMAGE", "ghcr.io/open-webui/open-webui:main"),
          E.get("MCP_IMAGE", "openwebui-rag-mcp-newapi:latest"), E.get("POSTGRES_IMAGE", "postgres:15-alpine"),
          E.get("REDIS_IMAGE", "redis:7-alpine")]
if PROFILES: images.append(E.get("MCP_OWUI_KEY_IMAGE", "openwebui-rag-mcp:latest"))
missing = [i for i in images if subprocess.run(["docker", "image", "inspect", i], capture_output=True).returncode != 0]
if missing: die("로컬에 없는 이미지: " + ", ".join(missing) + "  (docker pull / docker load / 빌드 후 다시 실행, README 1절)")
log("이미지 확인: " + ", ".join(images))

# ---------- 2~3. new-api 기동 ----------
log("PostgreSQL·Redis·new-api 기동")
compose("up", "-d", "--wait", "new-api")

class Session:
    def __init__(self, base):
        self.base = base; self.user_id = None
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))
    def call(self, method, path, body=None):
        headers = {"Content-Type": "application/json"} if body is not None else {}
        if self.user_id is not None: headers["New-Api-User"] = str(self.user_id)
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode() if body is not None else None,
                                     method=method, headers=headers)
        try:
            with self.opener.open(req, timeout=30) as r: text = r.read().decode()
        except urllib.error.HTTPError as e: text = e.read().decode()
        try: return json.loads(text)
        except json.JSONDecodeError: return {"success": False, "message": text[:200]}
    def login(self, user, pw):
        j = self.call("POST", "/api/user/login", {"username": user, "password": pw})
        if not j.get("success"): return False
        self.user_id = j["data"]["id"]; return True

for _ in range(60):
    try:
        if Session(LOCAL_NEWAPI).call("GET", "/api/status").get("success"): break
    except Exception: pass
    time.sleep(2)
else: die(f"{LOCAL_NEWAPI} 가 응답하지 않습니다")

# ---------- 4. new-api 설정 ----------
if not Session(LOCAL_NEWAPI).call("GET", "/api/setup")["data"]["status"]:
    j = Session(LOCAL_NEWAPI).call("POST", "/api/setup", {
        "username": E["NEW_API_ROOT_USER"], "password": E["NEW_API_ROOT_PASSWORD"],
        "confirmPassword": E["NEW_API_ROOT_PASSWORD"], "SelfUseModeEnabled": False, "DemoSiteEnabled": False})
    if not j.get("success"): die(f"new-api 초기 설정: {j.get('message')}")
    log(f"new-api 루트 계정 생성: {E['NEW_API_ROOT_USER']}")
root = Session(LOCAL_NEWAPI)
if not root.login(E["NEW_API_ROOT_USER"], E["NEW_API_ROOT_PASSWORD"]):
    die("루트 로그인 실패. 이미 초기화된 new-api 라면 .env 의 NEW_API_ROOT_PASSWORD 가 그때 값과 같아야 합니다")

def psql(sql):
    r = compose("exec", "-T", "postgres", "psql", "-U", E.get("PG_USER", "newapi"), "-d", E.get("PG_DB", "newapi"),
                "-v", "ON_ERROR_STOP=1", "-c", sql, check=False, capture=True)
    if r.returncode != 0: die(f"psql: {r.stderr.strip()}")

if E.get("NEW_API_ROOT_EMAIL"):
    # 관리자 API 는 이메일을 저장하지 않으므로 DB 에 직접 넣는다 (OIDC 로그인에 email 클레임이 필요)
    psql(f"update users set email = '{E['NEW_API_ROOT_EMAIL']}' where username = '{E['NEW_API_ROOT_USER']}';")
    log(f"루트 이메일: {E['NEW_API_ROOT_EMAIL']}")

svc = Session(LOCAL_NEWAPI)
if not svc.login(E["NEW_API_SERVICE_USER"], E["NEW_API_SERVICE_PASSWORD"]):
    j = root.call("POST", "/api/user/", {"username": E["NEW_API_SERVICE_USER"], "password": E["NEW_API_SERVICE_PASSWORD"],
                                         "display_name": "Open WebUI service"})
    if not j.get("success"): die(f"서비스 계정 생성: {j.get('message')}")
    if not svc.login(E["NEW_API_SERVICE_USER"], E["NEW_API_SERVICE_PASSWORD"]): die("서비스 계정 로그인 실패")
    log(f"서비스 계정 생성: {E['NEW_API_SERVICE_USER']}")

def items(j):
    d = j.get("data")
    return d.get("items", d) if isinstance(d, dict) else (d or [])

tok = next((t for t in items(svc.call("GET", "/api/token/?p=1&page_size=100")) if t["name"] == "openwebui-service"), None)
if tok is None:
    j = svc.call("POST", "/api/token/", {"name": "openwebui-service", "unlimited_quota": True, "expired_time": -1, "remain_quota": 0})
    if not j.get("success"): die(f"서비스 토큰 생성: {j.get('message')}")
    tok = next(t for t in items(svc.call("GET", "/api/token/?p=1&page_size=100")) if t["name"] == "openwebui-service")
    log("서비스 토큰 생성: openwebui-service (쿼터 무제한, 만료 없음)")
j = svc.call("POST", f"/api/token/{tok['id']}/key")
service_key = j["data"] if isinstance(j["data"], str) else j["data"]["key"]

def opt(k, v):
    j = root.call("PUT", "/api/option/", {"key": k, "value": v})
    if not j.get("success"): die(f"new-api 옵션 {k}: {j.get('message')}")

opt("ServerAddress", PUBLIC_NEWAPI)                       # OIDC issuer
opt("openwebui.service_api_key", service_key)             # relay: Open WebUI 서비스 키
opt("openwebui.jwt_secret", E["OWUI_JWT_SECRET"])         # relay: 사용자 정보 서명 JWT
opt("openwebui.jwt_issuer", "open-webui")
opt("openwebui.allow_unsigned_requests", True)
opt("openwebui.relay_enabled", True)
opt("oauth2.client_id", E.get("OIDC_CLIENT_ID", "openwebui"))   # OIDC: Open WebUI 로그인 클라이언트
opt("oauth2.client_secret", E["OIDC_CLIENT_SECRET"])
opt("oauth2.redirect_uri", PUBLIC_OWUI + "/oauth/oidc/login/callback")
opt("oauth2.app_url", PUBLIC_OWUI)
opt("oauth2.post_logout_redirect_uri", PUBLIC_OWUI + "/auth")
opt("oauth2.enabled", True)
opt("oauth2.token_from_key_client_id", E.get("OIDC_MCP_CLIENT_ID", "openwebui-mcp"))   # MCP: 키 → 토큰
opt("oauth2.token_from_key_enabled", True)
log(f"new-api 설정 완료 (issuer={PUBLIC_NEWAPI}, OIDC client={E.get('OIDC_CLIENT_ID', 'openwebui')}, "
    f"MCP client={E.get('OIDC_MCP_CLIENT_ID', 'openwebui-mcp')})")

if E.get("OWUI_SERVICE_KEY") != service_key:
    write_env({"OWUI_SERVICE_KEY": service_key}); E["OWUI_SERVICE_KEY"] = service_key
    log("OWUI_SERVICE_KEY 를 .env 에 기록")

# ---------- 5. Open WebUI·MCP 기동 ----------
log("Open WebUI·MCP 서버 기동")
compose("up", "-d", "--wait")

# ---------- 6. 연동 확인 ----------
class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **kw): return None

def http(method, url, body=None, follow=True):
    opener = urllib.request.build_opener(*([] if follow else [NoRedirect()]))
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None, method=method,
                                 headers={"Content-Type": "application/json"} if body is not None else {})
    try:
        with opener.open(req, timeout=30) as r: return r.status, r.headers, r.read().decode()
    except urllib.error.HTTPError as e: return e.code, e.headers, e.read().decode()

r = compose("exec", "-T", "open-webui", "python3", "-c",
            f"import urllib.request;urllib.request.urlopen('{PUBLIC_NEWAPI}/.well-known/openid-configuration',timeout=10)",
            check=False, capture=True)
if r.returncode != 0: die(f"Open WebUI 컨테이너에서 {PUBLIC_NEWAPI} 에 닿지 않습니다 (PUBLIC_HOST, README 2절)")
log("확인: Open WebUI → new-api OIDC discovery")

st, h, _ = http("GET", LOCAL_OWUI + "/oauth/oidc/login", follow=False)
loc = h.get("Location") or ""
if st not in (302, 303, 307) or not loc.startswith(PUBLIC_NEWAPI + "/oauth2/authorize"):
    die(f"Open WebUI 로그인이 new-api 로 리다이렉트되지 않습니다 (HTTP {st}, Location={loc})")
log("확인: Open WebUI 로그인 → new-api /oauth2/authorize")

st, _, text = http("POST", LOCAL_OWUI + "/api/v1/auths/oauth/oidc/token/exchange", {"token": "bootstrap-probe"})
if st == 403 and "disabled" in text.lower(): die("Open WebUI 토큰 교환이 꺼져 있습니다 (ENABLE_OAUTH_TOKEN_EXCHANGE)")
if st == 404: die("Open WebUI 에 oidc provider 가 등록되지 않았습니다")
log("확인: Open WebUI 토큰 교환 활성")

st, _, text = http("POST", LOCAL_NEWAPI + "/oauth2/token-from-key", None)
if st == 403 and "feature_disabled" in text: die("new-api 'API 키로 토큰 발급' 기능이 꺼져 있습니다")
if st == 404: die("new-api 에 /oauth2/token-from-key 가 없습니다 (이미지 버전 확인)")
log("확인: new-api /oauth2/token-from-key 활성")

r = compose("exec", "-T", "mcp-rag", "python", "/smoke_mcp.py", "sk-bootstrap-probe", check=False, capture=True)
if "newapi API 키가 유효하지 않습니다" not in r.stdout:
    die(f"MCP 서버 → new-api 경로 확인 실패:\n{r.stdout[-800:]}{r.stderr[-400:]}")
log("확인: MCP 서버 → new-api (잘못된 키를 정상적으로 거부)")

print()
log("완료")
log(f"  new-api   {PUBLIC_NEWAPI}   (관리자 {E['NEW_API_ROOT_USER']}" +
    (f" / 비밀번호 {E['NEW_API_ROOT_PASSWORD']} — .env 에 저장됨)" if generated_root_pw else ")"))
log(f"  Open WebUI {PUBLIC_OWUI}")
log(f"  MCP       http://{'<이 호스트>' if E.get('MCP_BIND', '127.0.0.1') != '127.0.0.1' else '127.0.0.1'}:{E.get('MCP_PORT', '8000')}/mcp")
log("다음: README 5절 (채널 추가, 사용자 준비, 지식 베이스, MCP 확인). 재시작은 docker compose up -d")
PY
