"""MCP server exposing Open WebUI knowledge bases as RAG search tools."""

import functools
import os

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from openwebui_rag_mcp.client import OpenWebUIClient, OpenWebUIError

DEFAULT_K = 5

mcp = MCPServer("openwebui-rag")


def _http_mode() -> bool:
    return os.environ.get("MCP_TRANSPORT", "stdio") == "http"


def get_api_key(ctx) -> str:
    """HTTP mode: Bearer token from the request headers. Otherwise OPENWEBUI_API_KEY."""
    if _http_mode():
        headers = getattr(ctx, "headers", None) or {}
        auth = headers.get("authorization") or headers.get("Authorization") or ""
        if auth.lower().startswith("bearer ") and auth[7:].strip():
            return auth[7:].strip()
    key = os.environ.get("OPENWEBUI_API_KEY")
    if key and not _http_mode():
        return key
    raise OpenWebUIError(
        "No API key provided. Send 'Authorization: Bearer <key>' (http mode) "
        "or set OPENWEBUI_API_KEY (stdio mode)."
    )


def _client(ctx) -> OpenWebUIClient:
    url = os.environ.get("OPENWEBUI_URL")
    if not url:
        raise OpenWebUIError("OPENWEBUI_URL is not set.")
    return OpenWebUIClient(url, get_api_key(ctx))


def _tool(fn):
    """Register fn as a tool; OpenWebUIError messages reach the client as tool errors.

    The module-level name stays the raw function (raises OpenWebUIError).
    """

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except OpenWebUIError as e:
            raise ToolError(str(e)) from e

    mcp.tool()(wrapper)
    return fn


@_tool
async def list_knowledge_bases(ctx: Context) -> str:
    """List the Open WebUI knowledge bases you can search.

    Call this first: it returns each knowledge base's id, name and description.
    Then pass the relevant ids to search_knowledge.
    """
    kbs = await _client(ctx).list_knowledge()
    if not kbs:
        return "No knowledge bases available."
    lines = []
    for kb in kbs:
        line = f"- {kb['id']}: {kb['name']}"
        if kb.get("description"):
            line += f" - {kb['description']}"
        lines.append(line)
    return "\n".join(lines)


@_tool
async def search_knowledge(
    query: str,
    knowledge_ids: list[str] | None = None,
    k: int | None = None,
    ctx: Context = None,
) -> str:
    """Search knowledge bases and return the most relevant text chunks with sources.

    Call list_knowledge_bases first, then pass the ids of the relevant bases as
    knowledge_ids. If omitted, all accessible bases are searched (only allowed
    when there are no more than MAX_COLLECTIONS of them). k is the number of
    chunks to return (default 5).
    """
    client = _client(ctx)
    if not knowledge_ids:
        knowledge_ids = [kb["id"] for kb in await client.list_knowledge()]
        cap = int(os.environ.get("MAX_COLLECTIONS", "20"))
        if len(knowledge_ids) > cap:
            raise OpenWebUIError(
                f"{len(knowledge_ids)} knowledge bases are accessible, more than the limit of {cap}. "
                "Call list_knowledge_bases and pass a narrower set via knowledge_ids."
            )
        if not knowledge_ids:
            return "No knowledge bases available."
    hits = await client.query(knowledge_ids, query, k or DEFAULT_K)
    if not hits:
        return "No results."
    return "\n\n".join(
        f"[{i}] {h['source']} (score {h['score']:.2f})\n{h['content']}" for i, h in enumerate(hits, 1)
    )


def main() -> None:
    if _http_mode():
        mcp.run(
            transport="streamable-http",
            host=os.environ.get("MCP_HOST", "0.0.0.0"),
            port=int(os.environ.get("MCP_PORT", "8000")),
        )
    else:
        mcp.run()


if __name__ == "__main__":
    main()
