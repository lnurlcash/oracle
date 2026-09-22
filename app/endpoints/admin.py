import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_session
from app.identity import ORACLE_PUBKEY_HEX
from app.models import Event
from app.services.oracle_service import (
    EventAlreadyExistsError,
    EventAlreadyResolvedError,
    EventNotFoundError,
    InvalidOutcomeError,
    announce_event,
    auto_resolve_btc_price_event,
    resolve_and_attest,
)

router = APIRouter(prefix="/admin", tags=["admin"])


# Curation is deliberately not public in v1 - see DESIGN.md's own trust-
# model section on why event categories stay narrow and operator-curated
# rather than open submission.
def require_admin(x_admin_key: str = Header(alias="X-Admin-Key")) -> None:
    if x_admin_key != settings.ADMIN_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid admin key.")


def get_http_client(request: Request) -> httpx.AsyncClient:
    return request.app.state.http_client


class AnnounceEventBody(BaseModel):
    event_id: str = Field(alias="eventId")
    category: str
    outcomes: list[str] = []
    maturity_time: int = Field(alias="maturityTime")
    # required for category == "btc-price" (announce_event itself
    # validates this) - fixed here, at announce time, not chosen later
    price_threshold_usd: float | None = Field(default=None, alias="priceThresholdUsd")

    model_config = {"populate_by_name": True}


class ResolveEventBody(BaseModel):
    outcome: str
    source: str


@router.post("/events", dependencies=[Depends(require_admin)])
async def create_event(
    body: AnnounceEventBody, session: AsyncSession = Depends(get_session)
) -> dict:
    try:
        event = await announce_event(
            session,
            ORACLE_PUBKEY_HEX,
            event_id=body.event_id,
            category=body.category,
            outcomes=body.outcomes,
            maturity_time=body.maturity_time,
            price_threshold_usd=body.price_threshold_usd,
        )
    except EventAlreadyExistsError:
        raise HTTPException(status_code=409, detail="An event with this id already exists.")
    except InvalidOutcomeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return event.to_public_dict()


@router.post("/events/{event_id}/resolve", dependencies=[Depends(require_admin)])
async def resolve_event(
    event_id: str, body: ResolveEventBody, session: AsyncSession = Depends(get_session)
) -> dict:
    try:
        event = await resolve_and_attest(
            session,
            settings.ORACLE_SECRET_KEY_HEX,
            event_id=event_id,
            outcome=body.outcome,
            source=body.source,
        )
    except EventNotFoundError:
        raise HTTPException(status_code=404, detail="No such event.")
    except EventAlreadyResolvedError:
        raise HTTPException(status_code=409, detail="This event is already resolved.")
    except InvalidOutcomeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _attestation_response(event)


@router.post("/events/{event_id}/auto-resolve", dependencies=[Depends(require_admin)])
async def auto_resolve_event(
    event_id: str,
    session: AsyncSession = Depends(get_session),
    http_client: httpx.AsyncClient = Depends(get_http_client),
) -> dict:
    """Only for `category: "btc-price"` events - fetches the real median
    price from `PRICE_SOURCE_URL` (see app/config.py, app/services/
    price_source.py) instead of an operator typing in "above"/"below" by
    hand. Still requires the admin key: this isn't "trustless", it's
    "someone still has to trigger it, but not decide the outcome"."""
    try:
        event = await auto_resolve_btc_price_event(
            session,
            settings.ORACLE_SECRET_KEY_HEX,
            http_client,
            settings.PRICE_SOURCE_URL,
            event_id=event_id,
        )
    except EventNotFoundError:
        raise HTTPException(status_code=404, detail="No such event.")
    except EventAlreadyResolvedError:
        raise HTTPException(status_code=409, detail="This event is already resolved.")
    except InvalidOutcomeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _attestation_response(event)


def _attestation_response(event: Event) -> dict:
    return {
        "eventId": event.event_id,
        "outcome": event.attested_outcome,
        "signatureHex": event.attestation_signature_hex,
        "resolvedAt": event.resolved_at,
        "source": event.resolution_source,
    }
