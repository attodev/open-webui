#!/usr/bin/env bash
# bootstrap.sh(new-api 설정) 다음에 실행한다. 여러 번 실행해도 안전하다(멱등).
#
#   phase pre  (Open WebUI 기동 전)
#     - 임베딩 채널: MOCK_UPSTREAM=1 이면 mock-embedding 채널, EMBEDDING_CHANNEL_KEY 가 있으면 실제 공급자 채널을 만든다
#   phase post (Open WebUI·MCP 기동 후)
#     - Open WebUI 토큰 교환이 켜져 있는지, Open WebUI 가 new-api discovery 에 닿는지 확인
#     - NEW_API_TEST_USERS 의 사용자를 OIDC 로 Open WebUI 에 한 번 로그인시킨다 (계정 생성)
#     - 첫 테스트 사용자에게 MCP 용 new-api 키(mcp)를 만들고, 샘플 지식 베이스를 올린 뒤 MCP 로 검색해 본다
#
#   ./bootstrap-rag.sh pre | post | all(기본)
#
# 요구: bash, python3(표준 라이브러리만), docker compose. 호스트에서 http://PUBLIC_HOST:포트 로 new-api·Open WebUI 에 닿아야 한다.
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
export RAG_PHASE="${1:-all}"
export COMPOSE_FILE_ARG="${COMPOSE_FILE_ARG:-docker-compose.yml}"

python3 - <<'PY'
import json, os, subprocess, sys, time, uuid
import urllib.request, urllib.error, urllib.parse
from http.cookiejar import CookieJar

E = os.environ
PHASE = E["RAG_PHASE"]
NEWAPI = f"http://{E['PUBLIC_HOST']}:{E.get('NEW_API_PORT', '3000')}"
OWUI = f"http://{E['PUBLIC_HOST']}:{E.get('OPEN_WEBUI_PORT', '8080')}"
EMB_MODEL = E.get("OWUI_EMBEDDING_MODEL") or "text-embedding-3-small"

def log(msg): print(f"[bootstrap-rag] {msg}", flush=True)
def warn(msg): print(f"[bootstrap-rag] 주의: {msg}", flush=True)
def die(msg): print(f"[bootstrap-rag] 실패: {msg}", file=sys.stderr); sys.exit(1)

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **kw): return None

class Http:
    """Cookie-keeping JSON client. follow=False returns redirects instead of following them."""
    def __init__(self, base="", follow=True):
        self.base = base; self.jar = CookieJar(); self.headers = {}
        handlers = [urllib.request.HTTPCookieProcessor(self.jar)]
        if not follow: handlers.append(NoRedirect())
        self.opener = urllib.request.build_opener(*handlers)
    def req(self, method, url, body=None, form=None, headers=None, raw=None, ctype=None):
        h = dict(self.headers); h.update(headers or {})
        data = None
        if body is not None: data = json.dumps(body).encode(); h["Content-Type"] = "application/json"
        if form is not None: data = urllib.parse.urlencode(form).encode(); h["Content-Type"] = "application/x-www-form-urlencoded"
        if raw is not None: data = raw; h["Content-Type"] = ctype
        r = urllib.request.Request(url if url.startswith("http") else self.base + url, data=data, method=method, headers=h)
        try:
            with self.opener.open(r, timeout=120) as resp:
                return resp.status, resp.headers, resp.read().decode(errors="replace")
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read().decode(errors="replace")
    def json(self, *a, **kw):
        st, _, text = self.req(*a, **kw)
        try: return st, json.loads(text)
        except json.JSONDecodeError: return st, {"_text": text[:300]}

def newapi_login(user, pw):
    s = Http(NEWAPI)
    st, j = s.json("POST", "/api/user/login", {"username": user, "password": pw})
    if not j.get("success"): return None
    s.headers["New-Api-User"] = str(j["data"]["id"])
    return s

def items(j):
    d = j.get("data")
    return d.get("items", []) if isinstance(d, dict) else (d or [])

def set_ratio(root, model):
    st, j = root.json("GET", "/api/option/")
    opts = {o["key"]: o["value"] for o in j.get("data", [])}
    m = json.loads(opts.get("ModelRatio") or "{}")
    if model not in m:
        m[model] = 0.01
        st, j = root.json("PUT", "/api/option/", {"key": "ModelRatio", "value": json.dumps(m)})
        if not j.get("success"): die(f"ModelRatio: {j.get('message')}")
        log(f"{model} 가격 비율 설정 (0.01)")

def ensure_channel(root, name, ch):
    st, j = root.json("GET", "/api/channel/?p=1&page_size=100")
    if any(c.get("name") == name for c in items(j)):
        log(f"채널 {name} 이미 있음"); return
    st, j = root.json("POST", "/api/channel/", {"mode": "single", "channel": ch})
    if not j.get("success"): die(f"채널 {name}: {j.get('message')}")
    log(f"채널 생성: {name} (모델 {ch['models']})")

def phase_pre():
    root = newapi_login(E["NEW_API_ROOT_USER"], E["NEW_API_ROOT_PASSWORD"])
    if not root: die(f"new-api 루트 로그인 실패 ({NEWAPI}). bootstrap.sh 를 먼저 실행했는지 확인")
    if E.get("MOCK_UPSTREAM", "0") == "1":
        ensure_channel(root, "mock-embedding", {
            "type": 1, "name": "mock-embedding", "key": "mock-key", "base_url": "http://mock-upstream:9100",
            "models": EMB_MODEL, "group": "default", "status": 1, "priority": 0, "weight": 0})
        set_ratio(root, EMB_MODEL)
    elif E.get("EMBEDDING_CHANNEL_KEY"):
        ch = {"type": int(E.get("EMBEDDING_CHANNEL_TYPE", "1")), "name": "embedding", "key": E["EMBEDDING_CHANNEL_KEY"],
              "models": EMB_MODEL, "group": "default", "status": 1, "priority": 0, "weight": 0}
        if E.get("EMBEDDING_CHANNEL_BASE_URL"): ch["base_url"] = E["EMBEDDING_CHANNEL_BASE_URL"]
        ensure_channel(root, "embedding", ch)
        set_ratio(root, EMB_MODEL)
    else:
        st, j = root.json("GET", "/api/channel/?p=1&page_size=100")
        if not any(EMB_MODEL in (c.get("models") or "").split(",") for c in items(j)):
            warn(f"임베딩 모델 {EMB_MODEL} 을 서비스하는 채널이 없습니다. .env 의 EMBEDDING_CHANNEL_KEY 를 채우거나 new-api 콘솔에서 추가하세요")

def wait_owui():
    for _ in range(90):
        try:
            st, _, _ = Http().req("GET", OWUI + "/health")
            if st == 200: return
        except Exception: pass
        time.sleep(2)
    die(f"Open WebUI 가 응답하지 않습니다: {OWUI}/health")

def check_owui():
    st, j = Http().json("POST", OWUI + "/api/v1/auths/oauth/oidc/token/exchange", {"token": "bootstrap-probe"})
    if st == 403 and "disabled" in json.dumps(j).lower():
        die("Open WebUI 토큰 교환이 꺼져 있습니다 (ENABLE_OAUTH_TOKEN_EXCHANGE)")
    if st == 404:
        die("Open WebUI 에 oidc provider 가 없습니다. OPENID_PROVIDER_URL 이 discovery 에 닿는지 확인 (README 2절)")
    log("Open WebUI 토큰 교환 활성 확인")
    cmd = ["docker", "compose", "-f", E["COMPOSE_FILE_ARG"], "exec", "-T", "open-webui", "python3", "-c",
           f"import urllib.request;urllib.request.urlopen('{NEWAPI}/.well-known/openid-configuration',timeout=10)"]
    if subprocess.run(cmd, capture_output=True).returncode != 0:
        die(f"Open WebUI 컨테이너에서 {NEWAPI} 에 닿지 않습니다 (README 2절 호스트 이름)")
    log("Open WebUI → new-api discovery 연결 확인")

def oidc_login(user, pw):
    """Browser-equivalent OIDC login so Open WebUI creates the account."""
    na = newapi_login(user, pw)
    if not na: return f"new-api 로그인 실패"
    ow = Http(OWUI, follow=False)
    st, h, _ = ow.req("GET", "/oauth/oidc/login")
    loc = h.get("Location") or ""
    if st not in (302, 303, 307) or "/oauth2/authorize" not in loc:
        return f"Open WebUI 로그인 리다이렉트 없음 (HTTP {st})"
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(loc).query))
    body = {k: q.get(k, "") for k in ("response_type", "client_id", "redirect_uri", "scope", "state", "nonce",
                                       "code_challenge", "code_challenge_method")}
    st, j = na.json("POST", "/api/oauth2/authorize", body)
    if not j.get("success"): return f"authorize 거부: {j.get('error')} {j.get('message', '')}"
    st, h, _ = ow.req("GET", j["data"]["redirect_url"])
    if st not in (302, 303, 307) or not any(c.name == "token" for c in ow.jar):
        return f"Open WebUI 콜백 실패 (HTTP {st}, Location={h.get('Location')})"
    return None

def user_key(user, pw, name="mcp"):
    s = newapi_login(user, pw)
    st, j = s.json("GET", "/api/token/?p=1&page_size=100")
    tok = next((t for t in items(j) if t["name"] == name), None)
    if tok is None:
        st, j = s.json("POST", "/api/token/", {"name": name, "unlimited_quota": True, "expired_time": -1, "remain_quota": 0})
        if not j.get("success"): die(f"{user} 의 {name} 키 생성: {j.get('message')}")
        st, j = s.json("GET", "/api/token/?p=1&page_size=100")
        tok = next(t for t in items(j) if t["name"] == name)
        log(f"{user} 에게 new-api 키 '{name}' 생성")
    st, j = s.json("POST", f"/api/token/{tok['id']}/key")
    k = j["data"] if isinstance(j["data"], str) else j["data"]["key"]
    return k if k.startswith("sk-") else "sk-" + k

def owui_jwt(key):
    st, j = Http().json("POST", NEWAPI + "/oauth2/token-from-key", headers={"Authorization": f"Bearer {key}"})
    if st != 200: die(f"token-from-key: HTTP {st} {j}")
    st, j = Http().json("POST", OWUI + "/api/v1/auths/oauth/oidc/token/exchange", {"token": j["access_token"]})
    if st != 200 or not j.get("token"): die(f"토큰 교환: HTTP {st} {j}")
    return j["token"]

SAMPLE = """Open WebUI RAG MCP 샘플 문서

이 문서는 bootstrap-rag.sh 가 MCP 검색 확인용으로 올린 샘플이다.
new-api 키로 MCP 서버에 접속하면 Open WebUI 지식 베이스를 검색할 수 있다.
토큰은 new-api 의 /oauth2/token-from-key 에서 발급받아 Open WebUI 토큰 교환으로 JWT 로 바꾼다.
"""

def seed_knowledge(jwt):
    s = Http(OWUI); s.headers["Authorization"] = f"Bearer {jwt}"
    st, j = s.json("GET", "/api/v1/knowledge/")
    kbs = j.get("items", j) if isinstance(j, dict) else j
    if any(k.get("name") == "MCP 샘플" for k in kbs or []):
        log("샘플 지식 베이스 이미 있음"); return
    st, kb = s.json("POST", "/api/v1/knowledge/create", {"name": "MCP 샘플", "description": "bootstrap-rag.sh 샘플"})
    if st != 200: die(f"지식 베이스 생성: HTTP {st} {kb}")
    boundary = uuid.uuid4().hex
    raw = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"mcp-sample.txt\"\r\n"
           f"Content-Type: text/plain\r\n\r\n{SAMPLE}\r\n--{boundary}--\r\n").encode()
    st, _, text = s.req("POST", "/api/v1/files/?process=true&process_in_background=false", raw=raw,
                        ctype=f"multipart/form-data; boundary={boundary}")
    if st != 200: die(f"파일 업로드/색인: HTTP {st} {text[:300]} (임베딩 채널 확인)")
    fid = json.loads(text)["id"]
    st, j = s.json("POST", f"/api/v1/knowledge/{kb['id']}/file/add", {"file_id": fid})
    if st != 200: die(f"지식 베이스에 파일 추가: HTTP {st} {j}")
    log("샘플 지식 베이스 'MCP 샘플' 생성 및 문서 색인")

def smoke(key):
    cmd = ["docker", "compose", "-f", E["COMPOSE_FILE_ARG"], "exec", "-T", "mcp-rag", "python", "/smoke_mcp.py", key, "토큰 교환"]
    r = subprocess.run(cmd, capture_output=True, text=True)
    print(r.stdout.replace(key, key[:6] + "…"))
    if r.returncode != 0: die(f"MCP 스모크 테스트 실패 {r.stderr[-500:]}")
    log("MCP 스모크 테스트 통과")

def phase_post():
    wait_owui()
    check_owui()
    users = [u.split(":") for u in E.get("NEW_API_TEST_USERS", "").split(",") if u.strip()]
    if E.get("OWUI_AUTO_LOGIN_TEST_USERS", "1") == "1":
        for name, pw, *_ in users:
            err = oidc_login(name, pw)
            if err: die(f"{name} OIDC 로그인: {err}")
            log(f"{name} Open WebUI 첫 로그인 완료 (계정 생성/갱신)")
    if not users:
        warn("NEW_API_TEST_USERS 가 비어 있어 샘플 지식·MCP 확인을 건너뜁니다"); return
    name, pw = users[0][0], users[0][1]
    key = user_key(name, pw)
    if E.get("SEED_SAMPLE_KNOWLEDGE", "1") == "1":
        seed_knowledge(owui_jwt(key))
    smoke(key)

if PHASE in ("pre", "all"): phase_pre()
if PHASE in ("post", "all"): phase_post()
log("완료")
PY
