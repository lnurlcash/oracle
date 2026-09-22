import httpx
import pytest

from app.services.price_source import PriceSourceError, fetch_median_usd

RATE_BODY = {
    "timestamp": "2026-09-22T12:00:00Z",
    "base": "BTC",
    "currency": "USD",
    "rates": {
        "median": 103452.10,
        "min": 103400.0,
        "max": 103500.0,
        "coinbase": 103452.10,
        "kraken": 103400.0,
        "bitfinex": 103500.0,
        "bitstamp": None,
        "binance": None,
        "coinmate": None,
        "gemini": None,
    },
}


def _client_returning(body: dict, status_code: int = 200) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json=body)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_fetch_median_usd_parses_a_real_shaped_response():
    async with _client_returning(RATE_BODY) as client:
        quote = await fetch_median_usd(client, "http://price.example.test")
    assert quote.median_usd == 103452.10
    assert quote.exchange_count == 3  # only the 3 non-null exchanges above
    assert quote.timestamp == "2026-09-22T12:00:00Z"
    assert "price.example.test" in quote.source_url


async def test_as_resolution_source_cites_something_checkable():
    async with _client_returning(RATE_BODY) as client:
        quote = await fetch_median_usd(client, "http://price.example.test")
    source = quote.as_resolution_source()
    assert "103,452.10" in source
    assert "3 exchanges" in source
    assert quote.source_url in source


async def test_raises_when_median_is_null():
    body = {**RATE_BODY, "rates": {**RATE_BODY["rates"], "median": None}}
    async with _client_returning(body) as client:
        with pytest.raises(PriceSourceError):
            await fetch_median_usd(client, "http://price.example.test")


async def test_raises_on_http_error():
    async with _client_returning({"detail": "Not Found"}, status_code=404) as client:
        with pytest.raises(PriceSourceError):
            await fetch_median_usd(client, "http://price.example.test")


async def test_raises_on_malformed_body():
    async with _client_returning({"nothing": "useful"}) as client:
        with pytest.raises(PriceSourceError):
            await fetch_median_usd(client, "http://price.example.test")


async def test_strips_a_trailing_slash_from_the_base_url():
    seen_urls = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_urls.append(str(request.url))
        return httpx.Response(200, json=RATE_BODY)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await fetch_median_usd(client, "http://price.example.test/")
    assert seen_urls == ["http://price.example.test/rate/USD"]
