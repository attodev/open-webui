import pytest


@pytest.mark.asyncio
async def test_harness_can_reach_a_bare_route(async_client):
    from fastapi import FastAPI

    app = FastAPI()

    @app.get('/ping')
    async def ping():
        return {'ok': True}

    async_client._transport = async_client._transport.__class__(app=app)
    res = await async_client.get('/ping')
    assert res.status_code == 200
    assert res.json() == {'ok': True}
