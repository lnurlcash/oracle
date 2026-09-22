"""Background recurring btc-price events: announces a fresh "will BTC be
above its own price right now" event every hour and every day, and
auto-resolves any btc-price event whose maturityTime has passed - the two
roadmap items DESIGN.md called out ("a scheduler for btc-price auto-
resolve", automatic event creation). Runs as a plain asyncio task inside
this app's own lifespan (app/main.py), on a sleep loop - no new
dependency (no APScheduler/celery/cron), matching this service's own
minimal-deps, no-migrations posture.

Deliberately still narrow, same spirit as the rest of this service's own
"Trust model": the ONLY thing this creates on its own is a plain "above/
below its own price at creation time" btc-price event - the single
lowest-ambiguity category here, mechanically resolved either way. It
never invents a sports/curated event, never picks its own threshold logic
beyond "the current price". On by default (Settings.SCHEDULER_ENABLED) -
see that field's own comment for why that's safe even before
PRICE_SOURCE_URL points at a real feed.

Both halves are individually idempotent and safe to re-run every tick:
- ensure_recurring_events keys each window's event by a deterministic id
  (btc-hourly-<hour>, btc-daily-<date>) and does nothing if that id
  already exists - a missed tick just means it's announced a bit later,
  never twice.
- auto_resolve_due_events only ever touches events that are still
  unresolved AND already past maturity; _sign_and_record_attestation
  itself (oracle_service.py) refuses a second resolution regardless.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.models import Event
from app.services.oracle_service import (
    EventAlreadyExistsError,
    EventAlreadyResolvedError,
    InvalidOutcomeError,
    announce_event,
    auto_resolve_btc_price_event,
)
from app.services.price_source import PriceSourceError, fetch_median_usd

logger = logging.getLogger("lnurlcash_oracle.scheduler")

HOURLY_LEAD_SECONDS = 3600
DAILY_LEAD_SECONDS = 86400


def hourly_event_id(now: datetime) -> str:
    return f"btc-hourly-{now.strftime('%Y-%m-%dT%H')}"


def daily_event_id(now: datetime) -> str:
    return f"btc-daily-{now.strftime('%Y-%m-%d')}"


async def _ensure_recurring_event(
    session_maker: async_sessionmaker,
    oracle_pubkey_hex: str,
    http_client: httpx.AsyncClient,
    price_source_url: str,
    *,
    event_id: str,
    lead_seconds: int,
) -> None:
    async with session_maker() as session:
        if await session.get(Event, event_id) is not None:
            return  # already announced this window - idempotent, safe every tick
        try:
            quote = await fetch_median_usd(http_client, price_source_url)
        except PriceSourceError as exc:
            logger.warning(
                "scheduler: could not fetch a price to announce %s: %s", event_id, exc
            )
            return
        try:
            await announce_event(
                session,
                oracle_pubkey_hex,
                event_id=event_id,
                category="btc-price",
                outcomes=[],  # announce_event forces ["above","below"] itself
                maturity_time=int(time.time()) + lead_seconds,
                price_threshold_usd=quote.median_usd,
            )
            logger.info(
                "scheduler: announced %s at threshold $%.2f", event_id, quote.median_usd
            )
        except EventAlreadyExistsError:
            pass  # lost a race with another tick/process - fine, same id either way
        except InvalidOutcomeError as exc:
            logger.warning("scheduler: could not announce %s: %s", event_id, exc)


async def ensure_recurring_events(
    session_maker: async_sessionmaker,
    oracle_pubkey_hex: str,
    http_client: httpx.AsyncClient,
    price_source_url: str,
) -> None:
    now = datetime.now(timezone.utc)
    await _ensure_recurring_event(
        session_maker,
        oracle_pubkey_hex,
        http_client,
        price_source_url,
        event_id=hourly_event_id(now),
        lead_seconds=HOURLY_LEAD_SECONDS,
    )
    await _ensure_recurring_event(
        session_maker,
        oracle_pubkey_hex,
        http_client,
        price_source_url,
        event_id=daily_event_id(now),
        lead_seconds=DAILY_LEAD_SECONDS,
    )


async def _due_btc_price_event_ids(session_maker: async_sessionmaker) -> list[str]:
    async with session_maker() as session:
        result = await session.execute(
            select(Event.event_id).where(
                Event.category == "btc-price",
                Event.attested_outcome.is_(None),
                Event.maturity_time <= int(time.time()),
            )
        )
        return [row[0] for row in result.all()]


async def auto_resolve_due_events(
    session_maker: async_sessionmaker,
    oracle_secret_hex: str,
    http_client: httpx.AsyncClient,
    price_source_url: str,
) -> None:
    for event_id in await _due_btc_price_event_ids(session_maker):
        async with session_maker() as session:
            try:
                await auto_resolve_btc_price_event(
                    session,
                    oracle_secret_hex,
                    http_client,
                    price_source_url,
                    event_id=event_id,
                )
                logger.info("scheduler: auto-resolved %s", event_id)
            except EventAlreadyResolvedError:
                pass  # lost a race with a manual resolve/another tick - fine
            except (PriceSourceError, InvalidOutcomeError) as exc:
                logger.warning("scheduler: could not auto-resolve %s: %s", event_id, exc)


async def scheduler_loop(
    session_maker: async_sessionmaker,
    oracle_pubkey_hex: str,
    oracle_secret_hex: str,
    http_client: httpx.AsyncClient,
    price_source_url: str,
    *,
    interval_seconds: int,
) -> None:
    logger.info("scheduler: started, checking every %ss", interval_seconds)
    while True:
        try:
            await ensure_recurring_events(
                session_maker, oracle_pubkey_hex, http_client, price_source_url
            )
            await auto_resolve_due_events(
                session_maker, oracle_secret_hex, http_client, price_source_url
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - one bad tick must never kill the loop
            logger.exception("scheduler: unexpected error in a scheduler tick")
        await asyncio.sleep(interval_seconds)
