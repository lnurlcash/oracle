# Design

A standalone Discreet Log Contract (DLC) oracle service. Settles bets
locked with [lnurl-wallet](../lnurl-wallet)'s `betlocker` addon, via the
oracle cryptography its sibling `dlc` addon already implements
(`src/addons/dlc/dlc.ts`).

**Independent of lnurl-mint by design.** Nothing about a `ct1`/`cw1` note
references a specific oracle - the mint never touches a note that happens
to be a bet, never knows one exists. This service never touches a note, a
mint, or funds; it only ever signs outcome attestations. Any oracle works
with any mint.

**Why not the mint itself?** Coupling them concentrates trust instead of
separating it: the mint already custodies note value; if it also decided
outcomes, one party would control both "how much is at stake" and "who
wins" - it wouldn't even need to forge an attestation to cheat, since it
already custodies the funds. Keeping oracle and mint independent (even
when the same team happens to run both) is what makes this worth building
as a DLC instead of a plain custodial bet.

## Trust model

This is a single, named, curated oracle - not multi-oracle, not
trustless. Whoever holds `ORACLE_SECRET_KEY_HEX` can attest to anything it
likes, honestly or not. The accountability this service actually
provides:

- **A public, permanent track record.** Every announcement and
  attestation stays retrievable forever via the API - a false attestation
  is a provable, permanent, cryptographically-tied-to-this-key lie, not
  deniable later.
- **A stated resolution source** on every attestation (`source` field) -
  not "trust us", but "here's exactly what was checked".
- **Narrow, low-ambiguity event categories** (see below) - the trust
  model gets weaker the more subjective the question is.

## Event lifecycle

```
ANNOUNCE (admin) → (wait until maturityTime) → RESOLVE + ATTEST (admin)
```

- **Announce**: a fresh nonce is generated for the event; its public point
  `R`, the oracle's own pubkey `P`, and the full outcome list are
  published immediately - all of it public information from this moment,
  none of it secret.
- **Resolve + attest**: after `maturityTime`, an outcome gets recorded
  either way this happens:
  - **Manually**: `POST /admin/events/{id}/resolve` with an outcome and a
    `source` citation the operator states themselves.
  - **Automatically**, for `category: "btc-price"` events only:
    `POST /admin/events/{id}/auto-resolve` fetches the current median
    BTC/USD price from `PRICE_SOURCE_URL` (see "Event categories" below)
    and resolves "above"/"below" against the threshold that was fixed at
    announce time - the real price and its own source are recorded
    verbatim in `resolution_source`, not just asserted. Still requires
    the admin key: this isn't "trustless", it's "someone still has to
    trigger it, but not decide the outcome".

  Either path signs the outcome with the event's own pre-announced nonce
  (`app/crypto/oracle.py::attest`) and **clears the nonce secret in the
  same database transaction** that records the attestation - both share
  one function for exactly this reason
  (`app/services/oracle_service.py::_sign_and_record_attestation`,
  `Event.nonce_secret_hex` set to `None`). This is the one hard invariant
  in the whole service: reusing a nonce across two attestations for the
  same event leaks the oracle's long-term key entirely, so it can never
  be a follow-up step that could be skipped or lost to a crash, or
  duplicated across two independently-maintained resolution paths.

**A third path exists for `btc-price` specifically: fully automatic, both
ends.** `app/services/scheduler.py` (on by default - `SCHEDULER_ENABLED`,
harmless until `PRICE_SOURCE_URL` points at a real feed) runs a
background loop that announces a fresh `btc-price` event every
hour and every day on its own (thresholded at whatever the real median
price is the moment it's created - "will BTC be above its own price right
now, an hour/a day from now") and auto-resolves any `btc-price` event
once its `maturityTime` passes, with no admin action at all. It only ever
touches events it created itself the same automatic way - a manually
announced `btc-price` event still needs a human (or a script) to call
`.../resolve` or `.../auto-resolve` explicitly. See `app/static/admin.html`
(served at `GET /admin`) for a small UI wrapping the manual paths.

Not built yet: user-submitted event proposals (curation stays
operator-only in v1), multi-oracle threshold schemes, the same kind of
automatic creation/resolution for any category besides `btc-price`. See
"Roadmap" below.

## Event categories (v1 scope)

Ordered by how mechanical the resolution is - deliberately no open-ended
"will X happen" freeform betting, which asks an oracle to be a judge
rather than a neutral clock:

| Category | Resolution | Ambiguity |
|---|---|---|
| `btc-price` | **Automated** - `POST .../auto-resolve` fetches the median BTC/USD price from `PRICE_SOURCE_URL` (`app/services/price_source.py`, lnbits-price-aggregator's own API shape - a real multi-exchange median, not a single point of failure) and compares it to the `priceThresholdUsd` fixed at announce time | Very low |
| Sports outcomes | A stated sports-data source, checked manually for now | Low |
| Curated public-event results | Operator-curated, source cited | Medium |

A `btc-price` event's `outcomes` are always exactly `["above", "below"]`
(enforced at announce time, not trusted from the caller) and it requires
a positive `priceThresholdUsd` in the same request. `PRICE_SOURCE_URL`
defaults to `https://price.lnurlcash.com` (not yet a live deployment) -
point it at a real `lnbits-price-aggregator` instance to actually use
auto-resolve (see `.env.example`).

With `SCHEDULER_ENABLED=true`, this oracle also announces its own
recurring `btc-hourly-<hour>` / `btc-daily-<date>` events on this same
category, with no operator action - see "Event lifecycle" above.

## API

Matches exactly what `lnurl-wallet`'s `dlc`/`betlocker` addons already
expect (`oraclePubkeyHex`, `nonceHex`, `outcomes: string[]` for an
announcement; `{outcome, signatureHex}` - 64-byte hex `R||s` - for an
attestation), so a future "browse open events" picker in Betlocker is a
fetch call, not a reshaping layer.

```
GET  /                                    → service info + trust model statement
GET  /admin                               → a small static admin UI (app/static/admin.html) -
                                              no auth to VIEW, every action it takes still needs
                                              the real X-Admin-Key, same as any other caller
GET  /oracle-pubkey                       → {oraclePubkeyHex}
GET  /events                              → [{eventId, category, outcomes, maturityTime, status}]
GET  /events/{eventId}/announcement       → {oraclePubkeyHex, nonceHex, outcomes, eventId, maturityTime}
GET  /events/{eventId}/attestation        → {outcome, signatureHex, resolvedAt, source}
                                              (404 until resolved - never a guess, never early)

POST /admin/events                        → announce (X-Admin-Key required)
POST /admin/events/{eventId}/resolve      → resolve + attest (X-Admin-Key required)
POST /admin/events/{eventId}/auto-resolve → resolve + attest via PRICE_SOURCE_URL,
                                              btc-price only (X-Admin-Key required)
```

## Key management

Dev-grade today: `ORACLE_SECRET_KEY_HEX` loads from the environment like
any other config value (`app/config.py`), same as this ecosystem's own
`timelock-service` loads `BIP46_XPRV`. **Not production-grade** - a real
deployment should move the long-term key behind an HSM or a hardened
signing service the API process never reads directly, with the public API
only ever able to *request* a signature over a fully-specified message.

The per-event nonce secret lifecycle (generate at announce, delete at
attest, in the attest transaction) is enforced today - see the lifecycle
section above.

## Crypto correctness

`app/crypto/oracle.py` is a from-scratch Python port of
`lnurl-wallet/src/addons/dlc/dlc.ts` - not `coincurve`'s own
`sign_schnorr` (which derives its own fresh nonce internally per BIP340's
default algorithm; the entire DLC mechanism depends on reusing the
pre-announced nonce instead). `tests/test_oracle_crypto.py` pins real
vectors generated by that TypeScript module and checks this Python port
matches byte-for-byte - the two must actually interoperate, not just each
be internally self-consistent.

Regenerate those vectors if `dlc.ts`'s algorithm ever changes: run a
one-off script there computing `schnorr.getPublicKey`, `outcomePoint`, and
`attest` for the fixed test keys `sk = '11'.repeat(32)` /
`'22'.repeat(32)`, and update the constants at the top of
`tests/test_oracle_crypto.py`.

This service's own output has also been verified, live, against:
- The wallet addon's `verifyAttestation` (a real HTTP announce → resolve →
  attest round trip, fed straight into `dlc.ts`).
- `lnurlcashkernel` (real Bitcoin Core, via `lnurl-mint`'s own dependency)
  - a note locked via `betlocker`'s `planBet` to this service's real
    announcement, redeemed with this service's real attestation, produces
    a witness Core's own script interpreter accepts.

## Stack

Python/FastAPI, matching `lnurl-mint`'s own stack (shared review muscle,
same author ecosystem) and `timelock-service`'s own project layout
(`app/endpoints`, `app/services`, `app/crypto`, `app/models.py`,
pydantic-settings, SQLAlchemy async + aiosqlite, `uv`, no migrations -
tables created on startup).

## Roadmap beyond v1

Shipped since the last pass: lnurl-wallet's Betlocker addon now fetches
`GET /events`/`GET /events/{id}/announcement`/`GET /events/{id}/attestation`
directly (`src/addons/dlc/oracleClient.ts`) instead of copy-pasted
pubkey/nonce/outcomes; and `app/services/scheduler.py` (on by default)
both announces and auto-resolves recurring `btc-price` events with no
operator action, once `PRICE_SOURCE_URL` points at a real feed.

- **Multi-oracle threshold** (2-of-3 must agree) - genuinely bigger scope,
  deliberately deferred, same call made for the wallet-side addon itself.
- **Automated sports-outcome resolution**, the same shape `btc-price`
  already has (a stated sports-data API instead of a stated price index),
  reducing "operator curates by hand" to just the medium-ambiguity
  curated-event category.
- **Announcement authenticity signature** - if this service is ever
  served over a channel without transport-level integrity (unlike plain
  HTTPS today), sign the announcement itself with the oracle's long-term
  key (matching dlcspecs' own `announcement_signature` field) so a client
  can verify it wasn't tampered with in transit independent of the
  channel.
