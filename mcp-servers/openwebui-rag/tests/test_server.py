from types import SimpleNamespace

import pytest

from openwebui_rag_mcp import server
from openwebui_rag_mcp.client import OpenWebUIError


class FakeClient:
    kbs = [
        {"id": "kb1", "name": "Docs", "description": "Product docs"},
        {"id": "kb2", "name": "HR", "description": ""},
    ]
    hits = [{"content": "hello world", "source": "a.pdf", "score": 0.9}]

    def __init__(self, base_url, api_key, timeout=30.0):
        FakeClient.last = self
        self.api_key = api_key
        self.query_calls = []

    async def list_knowledge(self):
        return self.kbs

    async def query(self, collection_names, query, k):
        self.query_calls.append((collection_names, query, k))
        return self.hits


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("OPENWEBUI_URL", "http://owui")
    monkeypatch.setenv("OPENWEBUI_API_KEY", "sk-env")
    monkeypatch.delenv("MAX_COLLECTIONS", raising=False)
    monkeypatch.delenv("MCP_TRANSPORT", raising=False)
    monkeypatch.setattr(server, "OpenWebUIClient", FakeClient)
    FakeClient.hits = [{"content": "hello world", "source": "a.pdf", "score": 0.9}]


def ctx(headers=None):
    return SimpleNamespace(headers=headers)


async def test_list_formats():
    out = await server.list_knowledge_bases(ctx())
    for s in ("kb1", "Docs", "kb2", "HR"):
        assert s in out


async def test_search_formats():
    out = await server.search_knowledge("q", knowledge_ids=["kb1"], ctx=ctx())
    assert out == "[1] a.pdf (score 0.90)\nhello world"
    assert FakeClient.last.query_calls == [(["kb1"], "q", server.DEFAULT_K)]


async def test_search_all_uses_accessible_ids():
    await server.search_knowledge("q", k=3, ctx=ctx())
    assert FakeClient.last.query_calls == [(["kb1", "kb2"], "q", 3)]


async def test_search_all_exceeds_cap(monkeypatch):
    monkeypatch.setenv("MAX_COLLECTIONS", "2")
    monkeypatch.setattr(FakeClient, "kbs", FakeClient.kbs + [{"id": "kb3", "name": "X", "description": ""}])
    with pytest.raises(OpenWebUIError, match="knowledge_ids"):
        await server.search_knowledge("q", ctx=ctx())


async def test_search_empty():
    FakeClient.hits = []
    assert await server.search_knowledge("q", knowledge_ids=["kb1"], ctx=ctx()) == "No results."


def test_http_mode_uses_header_key(monkeypatch):
    monkeypatch.setenv("MCP_TRANSPORT", "http")
    assert server.get_api_key(ctx({"authorization": "Bearer sk-user"})) == "sk-user"


def test_http_mode_no_header_ignores_env_key(monkeypatch):
    monkeypatch.setenv("MCP_TRANSPORT", "http")
    monkeypatch.setenv("OPENWEBUI_API_KEY", "sk-env")
    with pytest.raises(OpenWebUIError, match="No API key"):
        server.get_api_key(ctx())


def test_stdio_uses_env_key():
    assert server.get_api_key(ctx()) == "sk-env"


def test_no_key(monkeypatch):
    monkeypatch.delenv("OPENWEBUI_API_KEY")
    with pytest.raises(OpenWebUIError, match="No API key"):
        server.get_api_key(ctx())
