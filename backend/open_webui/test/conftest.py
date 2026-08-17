"""
Shared pytest fixtures for the backend test suite.

IMPORTANT: the env vars below must be set before any `open_webui.*` module
is imported, because `open_webui.config` reads them at import time (and can
trigger an Alembic migration run or a hard SystemExit if WEBUI_SECRET_KEY is
missing). conftest.py module-level code runs before pytest collects any
test module in this directory, which is what makes this reliable.

DATABASE_URL and ENABLE_DB_MIGRATIONS are unconditionally overridden (not
setdefault) to ensure test isolation: we always use a temp SQLite db with
ORM-only schema creation, never the developer's real database or Alembic
migrations, regardless of ambient shell environment.
"""

import os
import tempfile

os.environ.setdefault('WEBUI_SECRET_KEY', 'test-secret-key-not-for-production')
os.environ.setdefault('WEBUI_AUTH', 'True')
os.environ['ENABLE_DB_MIGRATIONS'] = 'False'  # schema created directly from ORM metadata instead

_TMP_DB_DIR = tempfile.mkdtemp(prefix='openwebui-test-db-')
os.environ['DATABASE_URL'] = f'sqlite:///{_TMP_DB_DIR}/test.db'

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from open_webui.internal.db import Base, engine

# Import all ORM models so they register with Base.metadata
from open_webui.models.auths import Auths  # noqa: F401
from open_webui.models.config import Config  # noqa: F401
from open_webui.models.users import Users  # noqa: F401
from open_webui.models.groups import Groups  # noqa: F401
from open_webui.models.oauth_sessions import OAuthSessions  # noqa: F401


@pytest.fixture(scope='function')
def db_engine():
    """Create every ORM table fresh for each test function, then drop them."""
    Base.metadata.create_all(bind=engine)
    yield engine
    Base.metadata.drop_all(bind=engine)


@pytest_asyncio.fixture
async def async_client(db_engine):
    """An httpx.AsyncClient with no app wired in yet.

    Each test module replaces `async_client._transport` with an
    `ASGITransport(app=<app under test>)` before making requests — see
    test_harness_smoke.py for the pattern. Tasks 4-8 build on this same
    fixture rather than importing the full `open_webui.main:app` (which
    would trigger the full startup lifespan, license fetch, and static-dir
    sync — unnecessary for router-level tests).
    """
    async with AsyncClient(transport=ASGITransport(app=None), base_url='http://test') as client:
        yield client
