from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # This oracle's own long-term identity key. Dev-grade for now: loaded
    # from the environment like any other secret here. A real deployment
    # should move this behind an HSM or a hardened signing service the API
    # process never reads directly - see DESIGN.md's own "Key management"
    # section. Generate one with `python -m app.gen_keypair`.
    ORACLE_SECRET_KEY_HEX: str

    # Curation is deliberately not public in v1 (see DESIGN.md) - propose/
    # resolve/attest all require this header (`X-Admin-Key`). Public GET
    # endpoints need nothing.
    ADMIN_API_KEY: str

    DATABASE_URL: str = "sqlite+aiosqlite:///./data/oracle.db"

    SERVICE_BASE_URL: str = "http://localhost:8420"

    # Backs automated resolution of `category: "btc-price"` events - see
    # app/services/price_source.py. lnbits-price-aggregator's own API
    # shape (GET {url}/rate/USD -> {rates: {median, min, max, <exchange>:
    # price, ...}}), median across several live exchanges rather than any
    # single one. Not yet a live deployment at this default - point it at
    # a real instance (e.g. http://localhost:8000 for a local
    # lnbits-price-aggregator) to actually use auto-resolve.
    PRICE_SOURCE_URL: str = "https://price.lnurlcash.com"


settings = Settings()  # type: ignore[call-arg]  # required fields come from .env
