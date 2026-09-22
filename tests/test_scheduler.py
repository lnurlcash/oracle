import time
from datetime import datetime, timezone

import httpx
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.crypto.oracle import generate_keypair, pubkey_from_secret, verify_attestation
from app.database import Base
from app.models import Event
from app.services.oracle_service import Attestation, announce_event
from app.services.scheduler import (
    auto_resolve_due_events,
    daily_event_id,
    ensure_recurring_events,
    hourly_event_id,
)

ORACLE_SECRET_HEX, ORACLE_PUBKEY_HEX = generate_keypair()
assert pubkey_from_secret(ORACLE_SECRET_HEX) == ORACLE_PUBKEY_HEX

RATE_BODY = {
    "timestamp": "2026-09-22T12:00:00Z",
    "base": "BTC",
    "currency": "USD",
    "rates": {"median": 100_000.0, "min": 99_900.0, "max": 100_100.0},
}


def _price_client(rates_body: dict = RATE_BODY) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=rates_body)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _failing_price_client() -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.fixture
async def session_maker():
    # a fresh in-memory db per test, StaticPool so every session opened
    # from this maker shares the same in-memory database - same pattern
    # conftest.py's own `session` fixture uses, just exposing the maker
    # itself rather than one already-opened session, since the scheduler
    # deliberately opens a fresh session per unit of work (see its own
    # top comment on why each half is independently idempotent)
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    yield maker
    await engine.dispose()


class TestEventIds:
    def test_hourly_and_daily_ids_are_distinct_and_deterministic(self):
        now = datetime(2026, 9, 22, 14, 37, tzinfo=timezone.utc)
        assert hourly_event_id(now) == "btc-hourly-2026-09-22T14"
        assert daily_event_id(now) == "btc-daily-2026-09-22"
        # same hour, different minute -> same id (deterministic per window)
        later_same_hour = datetime(2026, 9, 22, 14, 59, tzinfo=timezone.utc)
        assert hourly_event_id(later_same_hour) == hourly_event_id(now)


class TestEnsureRecurringEvents:
    async def test_announces_both_an_hourly_and_a_daily_event(self, session_maker):
        client = _price_client()
        await ensure_recurring_events(
            session_maker, ORACLE_PUBKEY_HEX, client, "https://price.example.com"
        )
        async with session_maker() as session:
            hourly = await session.get(Event, hourly_event_id(datetime.now(timezone.utc)))
            daily = await session.get(Event, daily_event_id(datetime.now(timezone.utc)))
        assert hourly is not None
        assert hourly.category == "btc-price"
        assert hourly.outcomes == ["above", "below"]
        assert hourly.price_threshold_usd == 100_000.0
        assert hourly.maturity_time <= int(time.time()) + 3600 + 5
        assert daily is not None
        assert daily.maturity_time <= int(time.time()) + 86400 + 5

    async def test_idempotent_across_repeated_ticks(self, session_maker):
        client = _price_client()
        await ensure_recurring_events(
            session_maker, ORACLE_PUBKEY_HEX, client, "https://price.example.com"
        )
        await ensure_recurring_events(
            session_maker, ORACLE_PUBKEY_HEX, client, "https://price.example.com"
        )  # must not raise EventAlreadyExistsError or create a duplicate
        async with session_maker() as session:
            from sqlalchemy import func, select

            count = (await session.execute(select(func.count()).select_from(Event))).scalar()
        assert count == 2  # exactly one hourly + one daily, not four

    async def test_does_not_announce_when_the_price_source_is_down(self, session_maker):
        client = _failing_price_client()
        await ensure_recurring_events(
            session_maker, ORACLE_PUBKEY_HEX, client, "https://price.example.com"
        )  # must not raise
        async with session_maker() as session:
            from sqlalchemy import func, select

            count = (await session.execute(select(func.count()).select_from(Event))).scalar()
        assert count == 0


class TestAutoResolveDueEvents:
    async def _announce(self, session_maker, event_id: str, maturity_time: int) -> None:
        async with session_maker() as session:
            await announce_event(
                session,
                ORACLE_PUBKEY_HEX,
                event_id=event_id,
                category="btc-price",
                outcomes=[],
                maturity_time=maturity_time,
                price_threshold_usd=90_000.0,
            )

    async def test_resolves_a_past_maturity_event_and_verifies(self, session_maker):
        await self._announce(session_maker, "btc-hourly-past", int(time.time()) + 61)
        # backdate maturity directly - announce_event itself refuses a
        # maturityTime that isn't far enough in the future, same guard a
        # real recurring event's own creation already passed at announce
        # time; this only fast-forwards past it for the test
        async with session_maker() as session:
            event = await session.get(Event, "btc-hourly-past")
            event.maturity_time = int(time.time()) - 1
            await session.commit()

        await auto_resolve_due_events(
            session_maker, ORACLE_SECRET_HEX, _price_client(), "https://price.example.com"
        )

        async with session_maker() as session:
            event = await session.get(Event, "btc-hourly-past")
        assert event.is_resolved
        assert event.attested_outcome == "above"  # 100_000 >= 90_000 threshold
        assert event.nonce_secret_hex is None
        attestation = Attestation(
            outcome=event.attested_outcome, signature_hex=event.attestation_signature_hex
        )
        assert verify_attestation(ORACLE_PUBKEY_HEX, event.nonce_pubkey_hex, attestation)

    async def test_leaves_a_not_yet_due_event_alone(self, session_maker):
        await self._announce(session_maker, "btc-hourly-future", int(time.time()) + 3600)
        await auto_resolve_due_events(
            session_maker, ORACLE_SECRET_HEX, _price_client(), "https://price.example.com"
        )
        async with session_maker() as session:
            event = await session.get(Event, "btc-hourly-future")
        assert not event.is_resolved

    async def test_a_failed_price_fetch_does_not_crash_the_batch(self, session_maker):
        await self._announce(session_maker, "btc-hourly-past2", int(time.time()) + 61)
        async with session_maker() as session:
            event = await session.get(Event, "btc-hourly-past2")
            event.maturity_time = int(time.time()) - 1
            await session.commit()

        await auto_resolve_due_events(
            session_maker,
            ORACLE_SECRET_HEX,
            _failing_price_client(),
            "https://price.example.com",
        )  # must not raise

        async with session_maker() as session:
            event = await session.get(Event, "btc-hourly-past2")
        assert not event.is_resolved  # left pending, to retry next tick
