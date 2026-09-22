"""Client for lnbits-price-aggregator's own API shape (`GET /rate/{currency}`
-> `{timestamp, base, currency, rates: {median, min, max, <exchange>: price
| null, ...}}`) - a real multi-exchange BTC price feed, not a single point
of failure. Used to automatically resolve `category: "btc-price"` events
(see app/services/oracle_service.py::auto_resolve_btc_price_event) instead
of an operator typing in "above"/"below" by hand.

Deliberately just an HTTP client against a *shape*, not a dependency on
that project's own code - any service speaking the same shape at
PRICE_SOURCE_URL works, including a real `price.lnurlcash.com` once one
exists.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx


class PriceSourceError(Exception):
    """The configured price source is unreachable, or returned something
    that doesn't look like a real price - never silently substitutes a
    guess."""


@dataclass(frozen=True)
class PriceQuote:
    median_usd: float
    exchange_count: int
    timestamp: str  # ISO 8601, exactly as the source reported it
    source_url: str

    def as_resolution_source(self) -> str:
        """A citation fit for Event.resolution_source - what an
        independent observer would need to spot-check this resolution
        after the fact."""
        return (
            f"{self.source_url}: median BTC/USD = ${self.median_usd:,.2f} "
            f"across {self.exchange_count} exchanges at {self.timestamp}"
        )


async def fetch_median_usd(
    client: httpx.AsyncClient, base_url: str
) -> PriceQuote:
    url = f"{base_url.rstrip('/')}/rate/USD"
    try:
        response = await client.get(url, timeout=10.0)
        response.raise_for_status()
        body = response.json()
        rates = body["rates"]
        median = rates["median"]
        if median is None:
            raise PriceSourceError(f"{url} has no median price available right now.")
        exchange_keys = (
            "coinbase",
            "kraken",
            "bitfinex",
            "bitstamp",
            "binance",
            "coinmate",
            "gemini",
        )
        exchange_count = sum(1 for k in exchange_keys if rates.get(k) is not None)
        return PriceQuote(
            median_usd=float(median),
            exchange_count=exchange_count,
            timestamp=str(body["timestamp"]),
            source_url=url,
        )
    except PriceSourceError:
        raise
    except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
        raise PriceSourceError(f"Could not get a price from {url}: {exc}") from exc
