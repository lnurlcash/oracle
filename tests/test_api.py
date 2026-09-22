import time

import httpx
import pytest

from app.crypto.oracle import Attestation, verify_attestation
from app.endpoints.admin import get_http_client
from app.main import app

ADMIN_HEADERS = {"X-Admin-Key": "test-admin-key"}


def _mock_price_client(rates_body: dict) -> None:
    """Overrides the admin router's own get_http_client dependency with a
    client that returns `rates_body` for GET /rate/USD - same pattern
    conftest.py's own `session` fixture already uses for get_session, so
    the auto-resolve endpoint can be tested without a real price source
    or triggering the app's real lifespan (which ASGITransport doesn't
    run by default)."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=rates_body)

    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    app.dependency_overrides[get_http_client] = lambda: mock_client


RATE_ABOVE_100K = {
    "timestamp": "2026-09-22T12:00:00Z",
    "base": "BTC",
    "currency": "USD",
    "rates": {
        "median": 103452.10,
        "min": 103400.0,
        "max": 103500.0,
        "coinbase": 103452.10,
        "kraken": 103400.0,
        "bitfinex": None,
        "bitstamp": None,
        "binance": None,
        "coinmate": None,
        "gemini": None,
    },
}

RATE_BELOW_100K = {**RATE_ABOVE_100K, "rates": {**RATE_ABOVE_100K["rates"], "median": 95000.0}}


def _future(seconds: int = 3600) -> int:
    return int(time.time()) + seconds


async def test_root_states_the_trust_model_plainly(client):
    resp = await client.get("/")
    body = resp.json()
    assert "oraclePubkeyHex" in body
    assert "not" in body["trustModel"].lower()


async def test_admin_ui_is_served_and_needs_no_auth_to_view(client):
    # the page itself is a public shell (same posture as GET /events or
    # FastAPI's own /docs) - every PRIVILEGED action it makes still checks
    # X-Admin-Key server-side, exactly as any other caller would
    resp = await client.get("/admin")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "X-Admin-Key" in resp.text


async def test_cors_allows_a_public_get_cross_origin(client):
    # a browser-based wallet at a different origin (e.g. lnurl-wallet's own
    # Betlocker oracleClient.ts) needs this header to read the response at
    # all - without it the request still succeeds server-side, but the
    # browser blocks the caller from ever seeing the body
    resp = await client.get("/events", headers={"Origin": "https://wallet.example.com"})
    assert resp.status_code == 200
    assert resp.headers["access-control-allow-origin"] == "*"


async def test_cors_refuses_a_cross_origin_preflight_for_admin_routes(client):
    # /admin/* must stay same-origin (curl, server-to-server, or this
    # service's own /admin page) - allow_methods is deliberately GET-only,
    # so Starlette's CORSMiddleware itself refuses to approve a preflight
    # for POST, which stops a browser from ever sending the real request,
    # on top of it needing the real X-Admin-Key regardless
    resp = await client.options(
        "/admin/events",
        headers={
            "Origin": "https://evil.example.com",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert resp.status_code == 400
    assert "GET" == resp.headers["access-control-allow-methods"]


async def test_full_lifecycle_announce_then_resolve_then_attest(client):
    maturity = _future()
    create = await client.post(
        "/admin/events",
        headers=ADMIN_HEADERS,
        json={
            "eventId": "test-event-1",
            "category": "test",
            "outcomes": ["yes", "no"],
            "maturityTime": maturity,
        },
    )
    assert create.status_code == 200
    assert create.json()["status"] == "announced"

    listing = await client.get("/events")
    assert any(e["eventId"] == "test-event-1" for e in listing.json())

    announcement = (await client.get("/events/test-event-1/announcement")).json()
    assert announcement["outcomes"] == ["yes", "no"]
    assert len(bytes.fromhex(announcement["oraclePubkeyHex"])) == 32
    assert len(bytes.fromhex(announcement["nonceHex"])) == 32

    # not resolved yet - never a guess, never early
    not_yet = await client.get("/events/test-event-1/attestation")
    assert not_yet.status_code == 404

    resolve = await client.post(
        "/admin/events/test-event-1/resolve",
        headers=ADMIN_HEADERS,
        json={"outcome": "yes", "source": "test fixture"},
    )
    assert resolve.status_code == 200
    assert resolve.json()["outcome"] == "yes"

    attestation = (await client.get("/events/test-event-1/attestation")).json()
    assert attestation["outcome"] == "yes"
    assert attestation["source"] == "test fixture"

    # the whole point: a real, independently-verifiable attestation
    assert (
        verify_attestation(
            announcement["oraclePubkeyHex"],
            announcement["nonceHex"],
            Attestation(
                outcome=attestation["outcome"],
                signature_hex=attestation["signatureHex"],
            ),
        )
        is True
    )


async def test_admin_endpoints_reject_a_missing_or_wrong_admin_key(client):
    wrong = await client.post(
        "/admin/events",
        headers={"X-Admin-Key": "not-the-real-key"},
        json={
            "eventId": "x",
            "category": "test",
            "outcomes": ["a", "b"],
            "maturityTime": _future(),
        },
    )
    assert wrong.status_code == 401


async def test_rejects_too_few_outcomes(client):
    resp = await client.post(
        "/admin/events",
        headers=ADMIN_HEADERS,
        json={
            "eventId": "one-outcome",
            "category": "test",
            "outcomes": ["only-one"],
            "maturityTime": _future(),
        },
    )
    assert resp.status_code == 400


async def test_rejects_a_maturity_time_too_close_to_now(client):
    resp = await client.post(
        "/admin/events",
        headers=ADMIN_HEADERS,
        json={
            "eventId": "too-soon",
            "category": "test",
            "outcomes": ["a", "b"],
            "maturityTime": int(time.time()) + 1,
        },
    )
    assert resp.status_code == 400


async def test_rejects_a_duplicate_event_id(client):
    body = {
        "eventId": "dup-event",
        "category": "test",
        "outcomes": ["a", "b"],
        "maturityTime": _future(),
    }
    first = await client.post("/admin/events", headers=ADMIN_HEADERS, json=body)
    assert first.status_code == 200
    second = await client.post("/admin/events", headers=ADMIN_HEADERS, json=body)
    assert second.status_code == 409


async def test_rejects_resolving_with_an_outcome_the_event_never_named(client):
    await client.post(
        "/admin/events",
        headers=ADMIN_HEADERS,
        json={
            "eventId": "bad-outcome-event",
            "category": "test",
            "outcomes": ["a", "b"],
            "maturityTime": _future(),
        },
    )
    resp = await client.post(
        "/admin/events/bad-outcome-event/resolve",
        headers=ADMIN_HEADERS,
        json={"outcome": "c", "source": "x"},
    )
    assert resp.status_code == 400


async def test_rejects_resolving_the_same_event_twice(client):
    await client.post(
        "/admin/events",
        headers=ADMIN_HEADERS,
        json={
            "eventId": "double-resolve",
            "category": "test",
            "outcomes": ["a", "b"],
            "maturityTime": _future(),
        },
    )
    first = await client.post(
        "/admin/events/double-resolve/resolve",
        headers=ADMIN_HEADERS,
        json={"outcome": "a", "source": "x"},
    )
    assert first.status_code == 200
    second = await client.post(
        "/admin/events/double-resolve/resolve",
        headers=ADMIN_HEADERS,
        json={"outcome": "b", "source": "y"},
    )
    assert second.status_code == 409


async def test_404_for_an_unknown_event(client):
    assert (await client.get("/events/nonexistent/announcement")).status_code == 404
    assert (await client.get("/events/nonexistent/attestation")).status_code == 404


@pytest.mark.parametrize("outcome", ["a", "b"])
async def test_nonce_secret_is_cleared_after_attestation(client, session, outcome):
    """The one structural invariant that actually matters here: once
    resolved, this event's own nonce secret must be gone from the row a
    later request could read - see Event's own docstring on why reusing
    it would leak the oracle's long-term key."""
    from app.models import Event

    await client.post(
        "/admin/events",
        headers=ADMIN_HEADERS,
        json={
            "eventId": "nonce-clear-test",
            "category": "test",
            "outcomes": ["a", "b"],
            "maturityTime": _future(),
        },
    )
    before = await session.get(Event, "nonce-clear-test")
    assert before.nonce_secret_hex is not None

    await client.post(
        "/admin/events/nonce-clear-test/resolve",
        headers=ADMIN_HEADERS,
        json={"outcome": outcome, "source": "x"},
    )
    session.expire_all()
    after = await session.get(Event, "nonce-clear-test")
    assert after.nonce_secret_hex is None


async def test_btc_price_event_requires_a_threshold(client):
    resp = await client.post(
        "/admin/events",
        headers=ADMIN_HEADERS,
        json={
            "eventId": "no-threshold",
            "category": "btc-price",
            "outcomes": [],
            "maturityTime": _future(),
        },
    )
    assert resp.status_code == 400


async def test_btc_price_event_ignores_caller_supplied_outcomes(client):
    """auto_resolve_btc_price_event assumes exactly ["above", "below"] -
    the announce endpoint enforces that itself rather than trusting it."""
    resp = await client.post(
        "/admin/events",
        headers=ADMIN_HEADERS,
        json={
            "eventId": "btc-100k",
            "category": "btc-price",
            "outcomes": ["yes", "no", "maybe"],
            "maturityTime": _future(),
            "priceThresholdUsd": 100_000,
        },
    )
    assert resp.status_code == 200
    assert resp.json()["outcomes"] == ["above", "below"]
    assert resp.json()["priceThresholdUsd"] == 100_000

    announcement = (await client.get("/events/btc-100k/announcement")).json()
    assert announcement["priceThresholdUsd"] == 100_000


async def test_a_non_btc_price_event_rejects_a_threshold(client):
    resp = await client.post(
        "/admin/events",
        headers=ADMIN_HEADERS,
        json={
            "eventId": "not-a-price-event",
            "category": "sports",
            "outcomes": ["a", "b"],
            "maturityTime": _future(),
            "priceThresholdUsd": 100_000,
        },
    )
    assert resp.status_code == 400


async def test_auto_resolve_above_threshold(client, session):
    _mock_price_client(RATE_ABOVE_100K)  # median 103,452.10
    await client.post(
        "/admin/events",
        headers=ADMIN_HEADERS,
        json={
            "eventId": "btc-100k-above",
            "category": "btc-price",
            "outcomes": [],
            "maturityTime": _future(),
            "priceThresholdUsd": 100_000,
        },
    )
    resp = await client.post(
        "/admin/events/btc-100k-above/auto-resolve", headers=ADMIN_HEADERS
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["outcome"] == "above"
    assert "103,452.10" in body["source"]

    announcement = (await client.get("/events/btc-100k-above/announcement")).json()
    attestation = (await client.get("/events/btc-100k-above/attestation")).json()
    assert (
        verify_attestation(
            announcement["oraclePubkeyHex"],
            announcement["nonceHex"],
            Attestation(outcome=attestation["outcome"], signature_hex=attestation["signatureHex"]),
        )
        is True
    )


async def test_auto_resolve_below_threshold(client):
    _mock_price_client(RATE_BELOW_100K)  # median 95,000.00
    await client.post(
        "/admin/events",
        headers=ADMIN_HEADERS,
        json={
            "eventId": "btc-100k-below",
            "category": "btc-price",
            "outcomes": [],
            "maturityTime": _future(),
            "priceThresholdUsd": 100_000,
        },
    )
    resp = await client.post(
        "/admin/events/btc-100k-below/auto-resolve", headers=ADMIN_HEADERS
    )
    assert resp.status_code == 200
    assert resp.json()["outcome"] == "below"


async def test_auto_resolve_refuses_a_non_btc_price_event(client):
    await client.post(
        "/admin/events",
        headers=ADMIN_HEADERS,
        json={
            "eventId": "sports-event",
            "category": "sports",
            "outcomes": ["a", "b"],
            "maturityTime": _future(),
        },
    )
    resp = await client.post("/admin/events/sports-event/auto-resolve", headers=ADMIN_HEADERS)
    assert resp.status_code == 400


async def test_auto_resolve_propagates_a_price_source_failure(client):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"detail": "no data yet"})

    app.dependency_overrides[get_http_client] = lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    )
    await client.post(
        "/admin/events",
        headers=ADMIN_HEADERS,
        json={
            "eventId": "btc-source-down",
            "category": "btc-price",
            "outcomes": [],
            "maturityTime": _future(),
            "priceThresholdUsd": 100_000,
        },
    )
    resp = await client.post(
        "/admin/events/btc-source-down/auto-resolve", headers=ADMIN_HEADERS
    )
    assert resp.status_code == 400
    # never burned/resolved on a failed price fetch
    event = await client.get("/events/btc-source-down/attestation")
    assert event.status_code == 404


async def test_auto_resolve_cannot_double_resolve(client):
    _mock_price_client(RATE_ABOVE_100K)
    await client.post(
        "/admin/events",
        headers=ADMIN_HEADERS,
        json={
            "eventId": "btc-double",
            "category": "btc-price",
            "outcomes": [],
            "maturityTime": _future(),
            "priceThresholdUsd": 100_000,
        },
    )
    first = await client.post("/admin/events/btc-double/auto-resolve", headers=ADMIN_HEADERS)
    assert first.status_code == 200
    second = await client.post("/admin/events/btc-double/auto-resolve", headers=ADMIN_HEADERS)
    assert second.status_code == 409
