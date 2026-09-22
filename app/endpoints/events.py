from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.identity import ORACLE_PUBKEY_HEX
from app.services.oracle_service import EventNotFoundError, get_event, list_events

router = APIRouter(tags=["events"])


@router.get("/oracle-pubkey")
async def oracle_pubkey() -> dict:
    return {"oraclePubkeyHex": ORACLE_PUBKEY_HEX}


@router.get("/events")
async def get_events(session: AsyncSession = Depends(get_session)) -> list[dict]:
    events = await list_events(session)
    return [e.to_public_dict() for e in events]


@router.get("/events/{event_id}/announcement")
async def get_announcement(
    event_id: str, session: AsyncSession = Depends(get_session)
) -> dict:
    try:
        event = await get_event(session, event_id)
    except EventNotFoundError:
        raise HTTPException(status_code=404, detail="No such event.")
    # the first 5 fields are exactly the shape lnurl-wallet's betlocker
    # addon's planBet() takes - see DESIGN.md's own "API" section on why
    # that's deliberate. priceThresholdUsd is extra, informational only
    # (not needed for the DLC math itself, which only cares about the
    # outcome list) - included so a human or a future event browser can
    # see what actually governs a "btc-price" event's own resolution.
    body: dict = {
        "oraclePubkeyHex": event.oracle_pubkey_hex,
        "nonceHex": event.nonce_pubkey_hex,
        "outcomes": event.outcomes,
        "eventId": event.event_id,
        "maturityTime": event.maturity_time,
    }
    if event.price_threshold_usd is not None:
        body["priceThresholdUsd"] = event.price_threshold_usd
    return body


@router.get("/events/{event_id}/attestation")
async def get_attestation(
    event_id: str, session: AsyncSession = Depends(get_session)
) -> dict:
    try:
        event = await get_event(session, event_id)
    except EventNotFoundError:
        raise HTTPException(status_code=404, detail="No such event.")
    if not event.is_resolved:
        # never a guess, never early - 404 until a real attestation exists,
        # same as this event simply not existing yet from the caller's
        # point of view
        raise HTTPException(status_code=404, detail="Not resolved yet.")
    # exactly the shape lnurl-wallet's betlocker addon's buildRedeemCw1()
    # takes as `attestation`
    return {
        "outcome": event.attested_outcome,
        "signatureHex": event.attestation_signature_hex,
        "resolvedAt": event.resolved_at,
        "source": event.resolution_source,
    }
