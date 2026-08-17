import pytest
from httpx import ASGITransport

from open_webui.models.users import Users


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


@pytest.mark.asyncio
async def test_app_config_defaults_enable_user_management_to_true_for_authenticated_admin(db_engine):
    # `enable_user_management` gates the admin-only Add/Delete User buttons
    # on UserList.svelte, so it's only present in the authenticated block of
    # `features` (guarded by `user is not None`) -- it must never appear for
    # an unauthenticated request. Authenticate as an admin to exercise it.
    from open_webui.internal.db import AsyncSessionLocal
    from open_webui.main import app
    from open_webui.utils.auth import create_token
    from httpx import ASGITransport, AsyncClient

    async with AsyncSessionLocal() as db:
        user = await Users.insert_new_user(
            id='user-1', name='Admin User', email='admin@example.com', role='admin', db=db
        )

    token = create_token(data={'id': user.id})

    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
        res = await client.get('/api/config', headers={'Authorization': f'Bearer {token}'})

    assert res.json()['features']['enable_user_management'] is True


@pytest.mark.asyncio
async def test_app_config_omits_enable_user_management_when_unauthenticated(db_engine):
    # Companion to the authenticated-default test above: an unauthenticated
    # request must not see this admin-only flag at all.
    from open_webui.main import app
    from httpx import ASGITransport, AsyncClient

    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
        res = await client.get('/api/config')

    assert 'enable_user_management' not in res.json()['features']
