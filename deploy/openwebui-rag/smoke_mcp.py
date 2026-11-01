"""MCP 서버 동작 확인용 스크립트. MCP 컨테이너 안에서 실행한다.

    docker compose exec mcp-rag python /smoke_mcp.py sk-<new-api 키> "검색어"
    docker compose exec mcp-rag-owui-key python /smoke_mcp.py sk-<Open WebUI 키> "검색어"

지식 베이스 목록을 출력하고, 검색어가 있으면 접근 가능한 전체 지식 베이스에서 검색한다.
"""

import asyncio
import sys

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

URL = "http://127.0.0.1:8000/mcp"


async def main(key: str, query: str | None) -> int:
    http = httpx2.AsyncClient(headers={"Authorization": f"Bearer {key}"}, timeout=60)
    async with streamable_http_client(URL, http_client=http) as (read, write, *_):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            print("tools:", ", ".join(t.name for t in tools.tools))
            calls = [("list_knowledge_bases", {})]
            if query:
                calls.append(("search_knowledge", {"query": query}))
            failed = False
            for name, args in calls:
                res = await session.call_tool(name, args)
                failed |= bool(res.is_error)
                print(f"\n== {name} ({'ERROR' if res.is_error else 'ok'})")
                print(res.content[0].text if res.content else "")
            return 1 if failed else 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)
    sys.exit(asyncio.run(main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None)))
