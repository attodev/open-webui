#!/usr/bin/env bash
# 처음 한 번(또는 설정을 바꾼 뒤) 전체 연동을 처음부터 끝까지 자동으로 한다. 여러 번 실행해도 안전하다.
#
#   1. new-api(+DB·Redis, MOCK_UPSTREAM=1 이면 mock-upstream) 기동
#   2. bootstrap.sh      : new-api 설정, OWUI_SERVICE_KEY 를 .env 에 기록
#   3. bootstrap-rag.sh pre : 임베딩 채널
#   4. Open WebUI + MCP 서버 빌드·기동
#   5. bootstrap-rag.sh post: 연결 확인, 테스트 사용자 첫 로그인, 샘플 지식, MCP 스모크 테스트
#
# 연동이 끝난 뒤의 재시작은 docker compose -f docker-compose.run.yml up -d
set -euo pipefail
cd "$(dirname "$0")"
[ -f .env ] || { echo ".env 가 없습니다. cp .env.example .env 후 값을 채우세요." >&2; exit 1; }
set -a; . ./.env; set +a

PROFILES=()
if [ "${MOCK_UPSTREAM:-0}" = "1" ]; then PROFILES+=(--profile mock); fi
if [ "${OWUI_ENABLE_API_KEYS:-false}" = "true" ]; then PROFILES+=(--profile owui-key); fi
dc() { docker compose -f docker-compose.yml "${PROFILES[@]}" "$@"; }

echo "[setup] 1/5 new-api 기동"
if [ "${MOCK_UPSTREAM:-0}" = "1" ]; then dc up -d new-api mock-upstream; else dc up -d new-api; fi
echo "[setup] 2/5 bootstrap.sh"
./bootstrap.sh
echo "[setup] 3/5 bootstrap-rag.sh pre"
./bootstrap-rag.sh pre
echo "[setup] 4/5 Open WebUI + MCP 기동"
# bootstrap.sh 가 .env 의 OWUI_SERVICE_KEY 를 바꿨을 수 있으므로 다시 읽고, 컨테이너를 재생성한다
set -a; . ./.env; set +a
dc up -d --build --wait
echo "[setup] 5/5 bootstrap-rag.sh post"
./bootstrap-rag.sh post
echo "[setup] 완료. Open WebUI: http://${PUBLIC_HOST}:${OPEN_WEBUI_PORT:-8080}  new-api: http://${PUBLIC_HOST}:${NEW_API_PORT:-3000}  MCP: http://<호스트>:${MCP_PORT:-8000}/mcp"
echo "[setup] 이후 재시작: docker compose -f docker-compose.run.yml ${PROFILES[*]} up -d"
