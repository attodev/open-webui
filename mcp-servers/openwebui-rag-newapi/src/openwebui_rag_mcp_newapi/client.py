"""Async client for the Open WebUI HTTP APIs used by the RAG MCP server."""

import httpx


class OpenWebUIError(Exception):
    """Raised for any failure talking to Open WebUI, with a readable message."""


class OpenWebUIClient:
    def __init__(self, base_url: str, token_provider, timeout: float = 30.0):
        """token_provider needs `async get_token() -> str` and `async invalidate() -> None`."""
        self._base_url = base_url.rstrip("/")
        self._token_provider = token_provider
        self._timeout = timeout

    async def _send(self, method: str, path: str, **kwargs) -> httpx.Response:
        token = await self._token_provider.get_token()
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, headers={"Authorization": f"Bearer {token}"}
            ) as http:
                return await http.request(method, f"{self._base_url}{path}", **kwargs)
        except httpx.TimeoutException as e:
            raise OpenWebUIError(f"Open WebUI request timed out after {self._timeout}s") from e
        except httpx.HTTPError as e:
            raise OpenWebUIError(f"Could not reach Open WebUI at {self._base_url}: {e}") from e

    async def _request(self, method: str, path: str, **kwargs):
        resp = await self._send(method, path, **kwargs)
        if resp.status_code == 401:
            await self._token_provider.invalidate()
            resp = await self._send(method, path, **kwargs)
        if resp.status_code == 401:
            raise OpenWebUIError(
                "Open WebUI rejected the token (401) even after re-authenticating."
            )
        if resp.status_code in (403, 404):
            raise OpenWebUIError(
                f"No access to this resource ({resp.status_code}). "
                "The collection may not exist or your API key lacks access to it."
            )
        if resp.status_code >= 400:
            raise OpenWebUIError(f"Open WebUI returned HTTP {resp.status_code}: {resp.text[:200]}")
        try:
            return resp.json()
        except ValueError as e:
            raise OpenWebUIError("Open WebUI returned a non-JSON response") from e

    async def list_knowledge(self) -> list[dict]:
        collected: list[dict] = []
        page = 1
        while True:
            data = await self._request("GET", "/api/v1/knowledge/", params={"page": page})
            if isinstance(data, list):
                batch, total = data, None
            elif isinstance(data, dict):
                batch, total = data.get("items", []), data.get("total")
            else:
                raise OpenWebUIError("Open WebUI returned an unexpected response shape for the knowledge list")
            if not isinstance(batch, list):
                raise OpenWebUIError("Open WebUI returned an unexpected response shape for the knowledge list")
            collected.extend(batch)
            if not batch or total is None or len(collected) >= total:
                break
            page += 1
        return [
            {"id": k.get("id"), "name": k.get("name"), "description": k.get("description")}
            for k in collected
        ]

    async def query(self, collection_names: list[str], query: str, k: int | None) -> list[dict]:
        body: dict = {"collection_names": collection_names, "query": query}
        if k is not None:
            body["k"] = k
        data = await self._request("POST", "/api/v1/retrieval/query/collection", json=body)
        if not isinstance(data, dict):
            raise OpenWebUIError("Open WebUI returned an unexpected response shape for the query")
        docs = (data.get("documents") or [[]])[0]
        metas = (data.get("metadatas") or [[]])[0]
        dists = (data.get("distances") or [[]])[0]
        results = []
        for i, doc in enumerate(docs):
            meta = (metas[i] if i < len(metas) else None) or {}
            results.append(
                {
                    "content": doc,
                    "source": meta.get("name") or meta.get("source") or "unknown",
                    "score": dists[i] if i < len(dists) else 0.0,
                }
            )
        return sorted(results, key=lambda r: r["score"], reverse=True)
