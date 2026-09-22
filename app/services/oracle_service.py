from __future__ import annotations

import time

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.crypto.oracle import Attestation, attest, generate_keypair
from app.models import Event
from app.services.price_source import PriceSourceError, fetch_median_usd

MIN_OUTCOMES = 2
# a lead time too short leaves no real window for a wallet to lock a note
# against this event before it matures - mirrors lnurl-wallet's own
# timelocker addon's MIN_LEAD_SECONDS reasoning, generous here since a bet
# needs time for both the lock AND the later redeem
MIN_LEAD_SECONDS = 60

# the fixed, canonical outcome pair for an automatically-resolved
# "btc-price" event - auto_resolve_btc_price_event assumes exactly this
BTC_PRICE_OUTCOMES = ["above", "below"]


class EventAlreadyExistsError(Exception):
    pass


class EventNotFoundError(Exception):
    pass


class EventAlreadyResolvedError(Exception):
    pass


class InvalidOutcomeError(Exception):
    pass


async def announce_event(
    session: AsyncSession,
    oracle_pubkey_hex: str,
    *,
    event_id: str,
    category: str,
    outcomes: list[str],
    maturity_time: int,
    price_threshold_usd: float | None = None,
) -> Event:
    """Commits this oracle to a fresh nonce for `event_id`, ahead of the
    event resolving - the one irreversible-in-spirit step here: this
    service must never announce two different nonces for the same
    event_id (that alone doesn't leak anything, unlike nonce reuse across
    two ATTESTATIONS, but it would let this service quietly redefine an
    event after the fact, which is exactly the kind of thing an oracle's
    own track record is supposed to make impossible).

    For `category == "btc-price"`, `price_threshold_usd` is REQUIRED and
    fixed here, at announce time - not chosen later at resolution, which
    would let the oracle pick a convenient number after already knowing
    the real price. `outcomes` for this category is always exactly
    `["above", "below"]` (BTC_PRICE_OUTCOMES), enforced rather than
    trusted from the caller, since auto_resolve_btc_price_event assumes it."""
    outcomes = [o.strip() for o in outcomes if o.strip()]
    outcomes = list(dict.fromkeys(outcomes))  # de-dupe, keep order

    if category == "btc-price":
        if price_threshold_usd is None or price_threshold_usd <= 0:
            raise InvalidOutcomeError(
                'category "btc-price" requires a positive priceThresholdUsd.'
            )
        outcomes = list(BTC_PRICE_OUTCOMES)
    elif price_threshold_usd is not None:
        raise InvalidOutcomeError(
            'priceThresholdUsd is only meaningful for category "btc-price".'
        )

    if len(outcomes) < MIN_OUTCOMES:
        raise InvalidOutcomeError(f"Needs at least {MIN_OUTCOMES} distinct outcomes.")
    if maturity_time < time.time() + MIN_LEAD_SECONDS:
        raise InvalidOutcomeError("maturityTime must be far enough in the future.")

    existing = await session.get(Event, event_id)
    if existing is not None:
        raise EventAlreadyExistsError(event_id)

    nonce_secret_hex, nonce_pubkey_hex = generate_keypair()
    event = Event(
        event_id=event_id,
        category=category,
        outcomes=outcomes,
        maturity_time=maturity_time,
        created_at=int(time.time()),
        oracle_pubkey_hex=oracle_pubkey_hex,
        nonce_pubkey_hex=nonce_pubkey_hex,
        nonce_secret_hex=nonce_secret_hex,
        price_threshold_usd=price_threshold_usd,
    )
    session.add(event)
    await session.commit()
    return event


async def _sign_and_record_attestation(
    session: AsyncSession,
    oracle_secret_hex: str,
    event: Event,
    *,
    outcome: str,
    source: str,
) -> Event:
    """The one place `Event.nonce_secret_hex` is ever cleared - shared by
    every resolution path (manual, btc-price auto-resolve, and any future
    one) so the "clear the nonce in the SAME transaction as the
    attestation" invariant (see Event's own docstring) can't drift between
    two independently-maintained copies of this logic."""
    if event.is_resolved:
        raise EventAlreadyResolvedError(event.event_id)
    if outcome not in event.outcomes:
        raise InvalidOutcomeError(f'"{outcome}" is not one of this event\'s own outcomes.')
    assert event.nonce_secret_hex is not None  # true whenever not yet resolved

    attestation: Attestation = attest(oracle_secret_hex, event.nonce_secret_hex, outcome)

    event.attested_outcome = attestation.outcome
    event.attestation_signature_hex = attestation.signature_hex
    event.resolved_at = int(time.time())
    event.resolution_source = source
    event.nonce_secret_hex = None
    await session.commit()
    return event


async def resolve_and_attest(
    session: AsyncSession,
    oracle_secret_hex: str,
    *,
    event_id: str,
    outcome: str,
    source: str,
) -> Event:
    """The manual path: an operator states the outcome and cites a source
    themselves. See _sign_and_record_attestation for what actually
    happens."""
    event = await session.get(Event, event_id)
    if event is None:
        raise EventNotFoundError(event_id)
    return await _sign_and_record_attestation(
        session, oracle_secret_hex, event, outcome=outcome, source=source
    )


async def auto_resolve_btc_price_event(
    session: AsyncSession,
    oracle_secret_hex: str,
    http_client: httpx.AsyncClient,
    price_source_url: str,
    *,
    event_id: str,
) -> Event:
    """The automated path for `category == "btc-price"`: fetches the
    current median BTC/USD price from `price_source_url` (see
    app/services/price_source.py - lnbits-price-aggregator's own API
    shape, e.g. a real `price.lnurlcash.com` deployment), compares it to
    the threshold fixed at announce time, and resolves "above" or "below"
    - the real-world number an independent observer could re-check is
    recorded verbatim in the resulting attestation's own `source` field
    (PriceQuote.as_resolution_source), not just asserted."""
    event = await session.get(Event, event_id)
    if event is None:
        raise EventNotFoundError(event_id)
    if event.category != "btc-price" or event.price_threshold_usd is None:
        raise InvalidOutcomeError('Only "btc-price" events can be auto-resolved.')

    try:
        quote = await fetch_median_usd(http_client, price_source_url)
    except PriceSourceError as exc:
        raise InvalidOutcomeError(str(exc)) from exc

    outcome = "above" if quote.median_usd >= event.price_threshold_usd else "below"
    return await _sign_and_record_attestation(
        session,
        oracle_secret_hex,
        event,
        outcome=outcome,
        source=quote.as_resolution_source(),
    )


async def list_events(session: AsyncSession) -> list[Event]:
    result = await session.execute(select(Event).order_by(Event.created_at.desc()))
    return list(result.scalars())


async def get_event(session: AsyncSession, event_id: str) -> Event:
    event = await session.get(Event, event_id)
    if event is None:
        raise EventNotFoundError(event_id)
    return event
