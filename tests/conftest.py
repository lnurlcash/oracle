import os

os.environ.setdefault(
    "ORACLE_SECRET_KEY_HEX", "11" * 32
)  # a fixed, well-known test key - never used outside tests
os.environ.setdefault("ADMIN_API_KEY", "test-admin-key")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.database import Base, get_session
from app.endpoints.admin import get_http_client
from app.main import app

# ASGITransport doesn't run FastAPI's real lifespan (which is what sets
# app.state.http_client in production - see app/main.py), so every test
# needs get_http_client resolvable regardless of whether its own request
# path actually reaches a price-source call: FastAPI resolves a route's
# dependencies before running the route body, even for a request that
# short-circuits before ever touching http_client. A generic default here
# keeps that from being every individual test's problem; tests that care
# about a SPECIFIC price response install their own override on top (see
# test_api.py's own _mock_price_client).
_DEFAULT_RATE_BODY = {
    "timestamp": "2026-01-01T00:00:00Z",
    "base": "BTC",
    "currency": "USD",
    "rates": {"median": 100_000.0, "min": 100_000.0, "max": 100_000.0},
}


def _default_http_client_override() -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_DEFAULT_RATE_BODY)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.fixture
async def session():
    # a fresh in-memory database per test, StaticPool so the same
    # in-memory db is reused across the several connections a single test
    # opens (each :memory: connection is otherwise its own empty database)
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async def override_get_session():
        async with maker() as s:
            yield s

    app.dependency_overrides[get_session] = override_get_session
    async with maker() as s:
        yield s
    app.dependency_overrides.clear()
    await engine.dispose()


@pytest.fixture
async def client(session):
    app.dependency_overrides[get_http_client] = _default_http_client_override
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
