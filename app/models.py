from __future__ import annotations

from sqlalchemy import JSON
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class Event(Base):
    """One DLC event, from announcement through (eventually) attestation.

    `nonce_secret_hex` is the one field that MUST be cleared the instant
    this event is attested to - reusing a nonce across two attestations
    for the same event leaks the oracle's own long-term key entirely (see
    app/services/oracle_service.py's own resolve_and_attest, which clears
    it in the same transaction that records the attestation, not a
    follow-up step that could be skipped or lost to a crash)."""

    __tablename__ = "events"

    event_id: Mapped[str] = mapped_column(primary_key=True)
    category: Mapped[str]
    outcomes: Mapped[list[str]] = mapped_column(JSON)
    maturity_time: Mapped[int]
    created_at: Mapped[int]

    oracle_pubkey_hex: Mapped[str]
    nonce_pubkey_hex: Mapped[str]
    # populated at announce time, cleared (set to None) the instant this
    # event is attested - see the class docstring
    nonce_secret_hex: Mapped[str | None] = mapped_column(default=None)

    # only set (and only meaningful) for category == "btc-price" - fixed
    # at ANNOUNCE time, not chosen later, so the oracle can't pick a
    # convenient threshold after the fact. See
    # app/services/oracle_service.py::auto_resolve_btc_price_event.
    price_threshold_usd: Mapped[float | None] = mapped_column(default=None)

    # None until resolved
    attested_outcome: Mapped[str | None] = mapped_column(default=None)
    attestation_signature_hex: Mapped[str | None] = mapped_column(default=None)
    resolved_at: Mapped[int | None] = mapped_column(default=None)
    # a plain-text citation of what was actually looked at to resolve this
    # event (a URL, an index description, ...) - the one accountability
    # tool this service has beyond the signature itself, see DESIGN.md
    resolution_source: Mapped[str | None] = mapped_column(default=None)

    @property
    def is_resolved(self) -> bool:
        return self.attested_outcome is not None

    def to_public_dict(self) -> dict:
        body: dict = {
            "eventId": self.event_id,
            "category": self.category,
            "outcomes": self.outcomes,
            "maturityTime": self.maturity_time,
            "status": "resolved" if self.is_resolved else "announced",
        }
        if self.price_threshold_usd is not None:
            body["priceThresholdUsd"] = self.price_threshold_usd
        return body
