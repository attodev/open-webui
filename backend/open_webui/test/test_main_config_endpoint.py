import pytest
from httpx import ASGITransport


@pytest.mark.asyncio
async def test_app_config_exposes_newapi_sso_settings(db_engine, async_client, monkeypatch):
    # `open_webui/__init__.py` defines a CLI entrypoint function also named
    # `main`, which shadows the `open_webui.main` submodule as a package
    # attribute until that submodule has actually been imported. Import it
    # explicitly first so `monkeypatch.setattr('open_webui.main....')`
    # resolves to the module (and its app-config globals) rather than to
    # that unrelated function.
    import open_webui.main  # noqa: F401

    monkeypatch.setattr('open_webui.main.ENABLE_NEWAPI_SSO', True)
    monkeypatch.setattr('open_webui.main.NEWAPI_ENTRY_URL', 'https://newapi.example.com/console/token')

    from open_webui.main import app

    async_client._transport = ASGITransport(app=app)
    res = await async_client.get('/api/config')

    assert res.status_code == 200
    assert res.json()['newapi_sso'] == {'enable': True, 'entry_url': 'https://newapi.example.com/console/token'}


@pytest.mark.asyncio
async def test_app_config_hides_entry_url_when_newapi_sso_disabled(db_engine, async_client, monkeypatch):
    # Covers the disabled-path behavior: entry_url should be empty even when
    # NEWAPI_ENTRY_URL is configured, to prevent leaking a disabled
    # integration's URL to unauthenticated clients.
    import open_webui.main  # noqa: F401

    monkeypatch.setattr('open_webui.main.ENABLE_NEWAPI_SSO', False)
    monkeypatch.setattr('open_webui.main.NEWAPI_ENTRY_URL', 'https://newapi.example.com/console/token')

    from open_webui.main import app

    async_client._transport = ASGITransport(app=app)
    res = await async_client.get('/api/config')

    assert res.status_code == 200
    assert res.json()['newapi_sso'] == {'enable': False, 'entry_url': ''}
