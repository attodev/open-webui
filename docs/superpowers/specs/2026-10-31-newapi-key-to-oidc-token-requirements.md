# newapi 요구서: API 키 → OIDC 액세스 토큰 발급 엔드포인트

## 배경

Open WebUI는 newapi를 OIDC 제공자로 써서 로그인하고 있다. 외부 MCP 클라이언트(Claude Desktop, Cursor 등)에서 Open WebUI 지식 검색을 쓰게 하는 MCP 서버를 만드는 중이다. 사용자가 Open WebUI API 키를 따로 관리하지 않고, **이미 가진 newapi API 키 하나만으로** 인증되게 하려 한다.

Open WebUI에는 "OAuth 토큰 교환" 기능이 있다(`POST /api/v1/auths/oauth/{provider}/token/exchange`, `ENABLE_OAUTH_TOKEN_EXCHANGE=true`로 켬). 이 기능은 **OIDC 제공자가 발급한 액세스 토큰**을 받아 Open WebUI JWT로 바꿔 준다. 하지만 newapi API 키(`sk-…`)는 OIDC 액세스 토큰이 아니다. 그래서 newapi가 API 키를 받아 해당 사용자의 OIDC 액세스 토큰을 발급해 주는 엔드포인트가 필요하다.

전체 흐름:

```
MCP 클라이언트 --(newapi sk- 키)--> MCP 서버
MCP 서버 --POST (sk- 키)--> newapi [이 요구서의 엔드포인트] --> OIDC 액세스 토큰
MCP 서버 --POST {token}--> Open WebUI /api/v1/auths/oauth/{provider}/token/exchange
Open WebUI --GET userinfo (Bearer 액세스 토큰)--> newapi  (토큰 검증)
Open WebUI --> MCP 서버: Open WebUI JWT
```

## 요구 사항

### R1. 엔드포인트

- `POST /api/oidc/token-from-key`. 경로는 newapi 관례에 맞게 바꿔도 된다. 바꾸면 최종 경로를 알려 줄 것. MCP 서버는 경로를 설정값으로 받는다.
- 요청: `Authorization: Bearer sk-…` 헤더. 본문은 없다.
- 성공 응답 `200 application/json`:

  ```json
  { "access_token": "<string>", "token_type": "Bearer", "expires_in": 600 }
  ```

  `expires_in`은 초 단위이며 필수다.
- 실패 응답은 JSON `{ "error": "<code>", "message": "<사람이 읽을 문장>" }` 형식이다.
  - `401 invalid_key`: 키가 없거나, 존재하지 않거나, 비활성화되었거나, 만료되었다.
  - `403 user_disabled`: 키 주인 계정이 비활성화되었거나 차단되었다.
  - `429 rate_limited`: 호출 횟수 제한을 넘었다.

### R2. 키 유효성 판단

- 키가 존재하고, 활성 상태이고, 만료되지 않았으며, 키 주인 계정이 활성 상태일 때만 토큰을 발급한다.
- 키의 남은 사용량(quota)은 판단에 쓰지 않는다. 이 토큰은 LLM 호출이 아니라 신원 확인용이기 때문이다. *(우리 쪽 판단이며, 다르게 하고 싶으면 알려 줄 것.)*

### R3. 발급하는 토큰이 반드시 지켜야 할 조건 (가장 중요)

1. **userinfo에서 받아들여져야 한다.** newapi OIDC discovery 문서(Open WebUI의 `OPENID_PROVIDER_URL`이 가리키는 `/.well-known/openid-configuration`)의 `userinfo_endpoint`에 `Authorization: Bearer <access_token>`으로 요청하면 200이 와야 한다.
2. **userinfo 응답은 일반 OIDC 웹 로그인 때와 똑같은 사용자 정보를 돌려줘야 한다.**
   - `sub`: 같은 사용자라면 웹 로그인 때와 **완전히 같은 값**이어야 한다. Open WebUI는 이 `sub`로 기존 계정을 찾는다. 값이 다르면 "User not found" 403이 난다.
   - `email`: 필수다. Open WebUI가 이메일이 없으면 거부한다.
   - 그 밖에 웹 로그인 때 주던 클레임(`name`, 역할·그룹 클레임 등)이 있다면 똑같이 준다. Open WebUI가 토큰 교환 때도 역할과 그룹을 이 값으로 갱신하기 때문에, 빠지면 사용자의 역할이나 그룹이 바뀔 수 있다.
   - **역할·그룹 클레임은 userinfo 응답에 담겨야 한다.** 액세스 토큰이 불투명 문자열이면 Open WebUI는 토큰 안의 클레임을 읽지 못하고 userinfo 응답만 본다. 역할·그룹이 ID 토큰에만 있으면 토큰 교환 때 사용자의 역할이 내려가거나 그룹이 빠질 수 있다.
3. **스코프는 웹 로그인과 같은 수준이어야 한다** (`openid email profile`). 그 이상의 권한은 주지 않는다.
4. **수명은 짧게 한다.** 기본 10분을 권장하고, 최대 15분이다. MCP 서버가 만료 전에 다시 받는다.
5. **신원 확인 외 권한은 없어야 한다.** 이 토큰으로 newapi 관리 API나 LLM 중계 API를 쓸 수 없어야 한다. userinfo, 그리고 introspection 대상이 되는 것만 허용한다.
6. **client_id를 정한다.** 토큰을 어떤 OIDC 클라이언트에 발급한 것으로 할지 정한다. Open WebUI용 기존 클라이언트로 하거나, MCP 전용 클라이언트(예: `openwebui-mcp`)를 새로 만든다. 전용 클라이언트를 권장한다. 감사 로그에서 구분되고, 문제가 생기면 따로 끌 수 있기 때문이다. 정한 값은 알려 줄 것.

### R4. 토큰 introspection (선택이지만 권장)

Open WebUI에서 `OAUTH_TOKEN_EXCHANGE_TRUSTED_CLIENT_IDS`를 설정하면, Open WebUI가 introspection으로 토큰의 client_id를 확인해서 허용한 클라이언트가 발급받은 토큰만 교환해 준다. 이것을 쓰려면 다음이 필요하다.

- discovery 문서에 `introspection_endpoint`가 있어야 한다.
- RFC 7662 형식을 따른다. 요청은 `POST`, form 본문 `token=<access_token>&token_type_hint=access_token`, Open WebUI 클라이언트 자격증명으로 Basic 인증한다.
- 응답은 `{ "active": true, "client_id": "<R3-6에서 정한 값>", ... }` 형식이다. 만료되었거나 폐기된 토큰이면 `{ "active": false }`.

- **다른 클라이언트에 발급된 토큰도 확인할 수 있어야 한다.** R3-6에서 MCP 전용 client_id를 쓰더라도, **Open WebUI 클라이언트의 자격증명으로** 그 토큰을 introspection할 수 있어야 한다. 일부 구현은 자기 클라이언트에 발급된 토큰만 확인해 주므로 주의할 것.

이미 introspection을 지원한다면 이 토큰도 대상에 포함되기만 하면 된다.

### 참고: Open WebUI 쪽 계정 상태

토큰 교환 자체는 Open WebUI 계정의 활성 여부를 확인하지 않는다. 그래서 차단된 사용자를 막는 일은 newapi가 맡는다(R2). 다만 Open WebUI에서 승인 대기 상태인 계정이나 MFA가 걸린 계정은 교환해도 JWT를 받지 못한다.

### R5. 보안·운영

- **호출 횟수 제한:** 키별·IP별로 둔다. 예: 키당 분당 30회. MCP 서버가 토큰을 캐시하므로 정상 사용이라면 키당 10분에 1회 수준이다.
- **감사 로그:** 발급 시각, 사용자 id, 키 id(키 원문은 남기지 않는다), 요청 IP, client_id를 남긴다.
- **기능 스위치:** 관리자가 이 기능을 끌 수 있어야 한다. 꺼져 있으면 `403 feature_disabled`를 돌려준다.
- **로그 노출 금지:** 키 원문과 발급한 토큰은 로그에 남기지 않는다.

## 완료 확인 방법 (newapi 쪽 테스트)

1. 유효한 키로 호출하면 200과 함께 `access_token`, `expires_in`이 온다.
2. 그 토큰으로 userinfo를 호출하면 200이 오고, `sub`와 `email`이 같은 사용자가 웹 OIDC 로그인했을 때 받는 userinfo와 똑같다.
3. 비활성화된 키, 만료된 키, 없는 키는 401이 난다. 비활성화된 사용자는 403이 난다.
4. 토큰이 만료된 뒤 userinfo를 호출하면 401이 난다.
5. 그 토큰으로 newapi의 LLM 중계 API나 관리 API를 호출하면 거부된다.
6. (R4를 구현했다면) introspection이 `active: true`와 정한 client_id를 돌려준다.
7. 끝까지 확인: Open WebUI에서 `ENABLE_OAUTH_TOKEN_EXCHANGE=true`를 켠다. 웹 OIDC로 한 번 이상 로그인한 사용자의 토큰을 `POST /api/v1/auths/oauth/<provider>/token/exchange`에 `{"token": "<access_token>"}`로 보내면 200과 Open WebUI JWT가 온다. `<provider>`는 Open WebUI에 등록된 이름이며, 일반 OIDC 설정이면 `oidc`이다.

## 알려 줘야 할 것 (구현 후 MCP 쪽에 전달)

- 최종 엔드포인트 경로
- 토큰 기본 수명
- R3-6의 client_id
- R4 introspection 지원 여부
