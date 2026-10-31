import httpx
import pytest
import respx

from openwebui_rag_mcp.client import OpenWebUIClient, OpenWebUIError

BASE = "http://owui.test"


def make():
    return OpenWebUIClient(BASE, "sk-test")


def items(start, n):
    return [{"id": f"id{i}", "name": f"n{i}", "description": "d", "extra": 1} for i in range(start, start + n)]


@respx.mock
async def test_list_paginates():
    route = respx.get(f"{BASE}/api/v1/knowledge/")
    route.side_effect = [
        httpx.Response(200, json={"items": items(0, 30), "total": 35}),
        httpx.Response(200, json={"items": items(30, 5), "total": 35}),
    ]
    result = await make().list_knowledge()
    assert len(result) == 35
    assert set(result[0]) == {"id", "name", "description"}
    assert route.calls[1].request.url.params["page"] == "2"


@respx.mock
async def test_list_accepts_bare_list():
    respx.get(f"{BASE}/api/v1/knowledge/").mock(return_value=httpx.Response(200, json=items(0, 2)))
    result = await make().list_knowledge()
    assert [r["id"] for r in result] == ["id0", "id1"]


@respx.mock
async def test_query_flattens():
    respx.post(f"{BASE}/api/v1/retrieval/query/collection").mock(
        return_value=httpx.Response(
            200,
            json={
                "documents": [["a", "b"]],
                "metadatas": [[{"name": "f1.pdf"}, {"source": "f2"}]],
                "distances": [[0.4, 0.9]],
            },
        )
    )
    result = await make().query(["c1"], "q", 5)
    assert result == [
        {"content": "b", "source": "f2", "score": 0.9},
        {"content": "a", "source": "f1.pdf", "score": 0.4},
    ]


@respx.mock
async def test_query_source_unknown():
    respx.post(f"{BASE}/api/v1/retrieval/query/collection").mock(
        return_value=httpx.Response(
            200, json={"documents": [["a"]], "metadatas": [[{}]], "distances": [[0.1]]}
        )
    )
    result = await make().query(["c1"], "q", None)
    assert result[0]["source"] == "unknown"


@respx.mock
async def test_query_omits_k_when_none():
    route = respx.post(f"{BASE}/api/v1/retrieval/query/collection").mock(
        return_value=httpx.Response(200, json={"documents": [[]], "metadatas": [[]], "distances": [[]]})
    )
    await make().query(["c1"], "q", None)
    import json

    body = json.loads(route.calls[0].request.content)
    assert "k" not in body
    assert body == {"collection_names": ["c1"], "query": "q"}


@respx.mock
async def test_bearer_header_sent():
    route = respx.get(f"{BASE}/api/v1/knowledge/").mock(return_value=httpx.Response(200, json=[]))
    await make().list_knowledge()
    assert route.calls[0].request.headers["Authorization"] == "Bearer sk-test"


@respx.mock
async def test_401_maps_to_auth_error():
    respx.get(f"{BASE}/api/v1/knowledge/").mock(return_value=httpx.Response(401))
    with pytest.raises(OpenWebUIError, match="API key"):
        await make().list_knowledge()


@pytest.mark.parametrize("status", [403, 404])
@respx.mock
async def test_403_maps_to_access_error(status):
    respx.post(f"{BASE}/api/v1/retrieval/query/collection").mock(return_value=httpx.Response(status))
    with pytest.raises(OpenWebUIError, match="access"):
        await make().query(["c1"], "q", None)
