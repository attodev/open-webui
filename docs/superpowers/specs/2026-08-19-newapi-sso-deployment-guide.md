# OpenWebUI 배포 가이드 — new-api SSO 연동

이 문서는 **OpenWebUI 쪽**에서 new-api SSO 연동 기능을 켜기 위한 설정
방법을 정리한다. 관련 코드/버그 수정 내역은 다음 참고:

- `docs/superpowers/specs/2026-08-17-newapi-oauth2-sso-integration-design.md` (설계)
- `docs/superpowers/specs/2026-08-19-newapi-sso-testing-changes-summary.md` (변경 요약)

> **선행 조건**: 연동 코드는 `newapi` 브랜치에 있다. `main`에 아직
> 머지되지 않았다면 이 브랜치를 직접 빌드해서 써야 한다.

---

## 1. 처음부터 새로 설치하는 경우

### 1-1. 소스 준비

```bash
git clone git@github.com:attodev/open-webui.git
cd open-webui
git checkout newapi
```

### 1-2. new-api 쪽에서 먼저 받아둘 값

OpenWebUI를 설정하기 전에, new-api 관리자에게 아래 값을 받아둔다
(new-api 배포 가이드의 "관리 화면 설정" 참고):

- `client_id`
- `client_secret`
- new-api 접속 주소 (예: `https://newapi.example.com`)

그리고 OpenWebUI 쪽에서 쓸 콜백 URL(`https://<openwebui-도메인>/auth/newapi/callback`)을
미리 정해서 new-api 관리자에게 전달해 `redirect_uri`로 등록해달라고
요청한다. **양쪽 값이 정확히 일치해야 한다.**

### 1-3. Docker Compose로 띄우기 (권장)

공식 `Dockerfile`은 프론트엔드(SvelteKit)까지 한 번에 빌드해서 백엔드가
서빙하도록 되어 있다 — 별도로 프론트엔드를 신경 쓸 필요 없음(테스트
환경에서 우리가 겪었던 "프론트엔드 빌드 누락으로 404" 문제는 정식
Dockerfile을 쓰면 발생하지 않는다).

```yaml
# docker-compose.yaml (기존 파일을 기반으로 환경변수만 추가)
services:
  open-webui:
    build:
      context: .
      dockerfile: Dockerfile
    container_name: open-webui
    volumes:
      - open-webui:/app/backend/data
    ports:
      - "8080:8080"
    environment:
      - WEBUI_SECRET_KEY=<임의의 긴 랜덤 문자열>   # 반드시 설정. 세션 JWT 서명 키

      # --- new-api SSO 연동 ---
      - ENABLE_NEWAPI_SSO=true
      - NEWAPI_OAUTH_BASE_URL=https://newapi.example.com
      - NEWAPI_OAUTH_CLIENT_ID=<new-api에서 발급받은 client_id>
      - NEWAPI_OAUTH_CLIENT_SECRET=<new-api에서 발급받은 client_secret>
      - NEWAPI_ENTRY_URL=https://newapi.example.com

      # --- 필수: 실제 도메인 CORS 허용 ---
      - CORS_ALLOW_ORIGIN=https://chat.example.com   # OpenWebUI 자신의 실제 접속 도메인. localhost 아님!

      # --- 권장: 모델 접근 권한 이중 관리 방지 ---
      - BYPASS_MODEL_ACCESS_CONTROL=true   # new-api가 이미 그룹별 모델 접근을 통제하므로

    restart: unless-stopped

volumes:
  open-webui: {}
```

```bash
docker compose build
docker compose up -d
```

### 1-4. 관리자 계정 (첫 로그인)

- 로컬 회원가입/로그인 폼은 이 통합에서 의도적으로 안 쓴다 — new-api를
  통해서만 로그인한다.
- **가장 처음 SSO로 로그인하는 계정이 자동으로 OpenWebUI 관리자가
  된다** (신규 설치 기준). new-api 쪽 관리자 계정으로 먼저 한 번
  로그인해서 이 부트스트랩을 완료해두는 걸 권장.

---

## 2. 이미 운영 중인 OpenWebUI에 반영하는 경우

```bash
cd <기존 open-webui 소스 디렉토리>
git fetch origin
git checkout main          # 또는 현재 운영 중인 브랜치
git merge origin/newapi
```

### 반영 후 체크리스트

1. **환경변수 추가** — 위 1-3의 `ENABLE_NEWAPI_SSO` 이하 항목들을
   기존 컨테이너 설정에 추가.
2. **`CORS_ALLOW_ORIGIN` 재확인** — 기존 배포가 `localhost` 기준으로
   설정되어 있었다면(개발용 `dev.sh`를 그대로 쓰고 있었다면 특히),
   실제 서비스 도메인으로 바꿔야 한다. 이 값이 잘못되면 로그인은 되는데
   **채팅 응답이 "생각 중..."에서 멈추는 증상**으로 나타난다 (WebSocket
   핸드셰이크가 origin 불일치로 거부됨).
3. **기존 로컬 계정 처리** — 이 연동을 켜면 로컬 회원가입/로그인 폼이
   숨겨지지만, 기존에 로컬 계정으로 가입되어 있던 사용자들의 데이터는
   그대로 남아있다. new-api 계정과 자동으로 연결되지 않으므로, 이메일이
   겹치는 경우에만 `oauth.merge_accounts_by_email` 설정을 켜서 연결할
   수 있다 (계정별로 수동 확인 권장).
4. **관리자 역할 유지** — 기존에 로컬 admin 계정이 있었다면 SSO
   로그인과는 별개로 그대로 유지된다. new-api 쪽 계정에 admin 권한을
   자동으로 동기화하려면(`is_admin` 클레임), new-api 쪽에서 해당 사용자를
   관리자로 설정해두면 다음 SSO 로그인 시 자동으로 승격된다 (단,
   자동으로 강등되지는 않음 — 로컬에서 수동으로 admin을 준 계정은 계속
   유지됨).
5. **재시작 후 실제 로그인 테스트** — new-api 쪽에서 "채팅 앱에서
   열기" 버튼을 눌러 실제로 계정이 생성/로그인되는지, 모델 목록이
   보이는지, 채팅 응답이 오는지까지 순서대로 확인.

---

## 3. 독립된 컨테이너 환경에서 운영할 때 참고 사항

### 3-1. 프론트엔드는 백엔드와 한 컨테이너에서 같이 빌드/서빙된다

공식 `Dockerfile`은 멀티스테이지 빌드로 프론트엔드(Node/npm)를 먼저
빌드하고, 그 결과물을 백엔드 이미지에 포함시켜 백엔드가 직접 서빙한다.
**별도의 프론트엔드 컨테이너를 따로 띄울 필요가 없다.** (테스트
환경에서는 시간 단축을 위해 파이썬 컨테이너 하나만 띄우고 프론트를
수동으로 빌드해 넣었는데, 정식 배포는 Dockerfile 그대로 쓰면 이 문제
자체가 없다.)

### 3-2. new-api로의 네트워크 도달성

OpenWebUI 컨테이너가 new-api 컨테이너/서버로 서버-투-서버 호출을 한다
(`NEWAPI_OAUTH_BASE_URL` 대상). 같은 Docker 네트워크에 있지 않다면:

- 같은 서버의 다른 컨테이너: 호스트의 공인 IP 또는 Docker 브릿지 게이트웨이
  IP(`docker network inspect <네트워크>`로 확인, 보통 `172.17.0.1` 등) 사용.
- 완전히 다른 서버: new-api의 실제 접속 가능한 URL(리버스 프록시 뒤라면
  그 도메인) 사용. 방화벽에서 OpenWebUI → new-api 방향 아웃바운드 허용
  필요.

### 3-3. `WEBUI_SECRET_KEY`는 컨테이너 재생성 시에도 고정해야 함

이 값이 바뀌면 기존에 발급된 모든 세션(JWT)이 무효화된다. 컨테이너를
재생성/재배포할 때마다 값이 바뀌지 않도록 환경변수 파일이나 시크릿
관리 도구에 고정값으로 저장해둘 것.

### 3-4. 데이터 볼륨

`open-webui:/app/backend/data`(SQLite DB, 업로드 파일 등)를 반드시
볼륨으로 마운트할 것. 마운트하지 않으면 컨테이너를 재생성할 때마다
계정/대화 기록이 전부 사라진다.

---

## 4. 트러블슈팅 빠른 체크리스트

| 증상 | 원인/확인할 것 |
|---|---|
| new-api에서 버튼 눌렀는데 OpenWebUI 로그인 화면(폼)이 나옴 | `ENABLE_NEWAPI_SSO=true` 확인, `NEWAPI_OAUTH_CLIENT_ID/SECRET`이 new-api 쪽과 일치하는지 확인 |
| "계정 활성화 대기" 화면이 나옴 | 이 브랜치의 최신 수정(`role='user'` 고정)이 반영됐는지 확인. 이미 생성된 pending 계정은 관리자 패널에서 수동으로 활성화 |
| 로그인은 되는데 모델 목록이 비어있음 | `BYPASS_MODEL_ACCESS_CONTROL=true` 확인 (일반 사용자 계정 기준으로 테스트할 것 — admin은 원래 다 보임) |
| 채팅 보내면 "생각 중..."에서 멈춤 | `CORS_ALLOW_ORIGIN`이 실제 접속 도메인과 일치하는지 확인 (브라우저 콘솔에 WebSocket 에러 있는지 확인) |
| `/auth`로 리다이렉트된 후 404 | 프론트엔드가 빌드/서빙되고 있는지 확인 (공식 Dockerfile을 쓰면 발생 안 함) |
| `invalid_userinfo` 오류 | new-api 쪽 `/oauth2/userinfo`가 `sub`를 못 주는 경우 — new-api 쪽 문제이니 new-api 관리자에게 문의 |
