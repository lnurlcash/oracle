from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI

from app import __version__
from app.database import init_db
from app.endpoints import admin, events
from app.identity import ORACLE_PUBKEY_HEX


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    # one shared client for the app's whole lifetime (connection pooling,
    # not a fresh TCP/TLS handshake per price-source request) - read back
    # via app.endpoints.admin's own get_http_client dependency
    async with httpx.AsyncClient() as client:
        app.state.http_client = client
        yield


app = FastAPI(
    title="lnurlcash-oracle",
    version=__version__,
    description=(
        "A single-oracle Discreet Log Contract service. Not trustless - "
        "whoever holds this oracle's secret key can attest to anything it "
        "likes; see DESIGN.md for the accountability this service does and "
        "does not provide."
    ),
    lifespan=lifespan,
)

app.include_router(events.router)
app.include_router(admin.router)


@app.get("/")
async def root() -> dict:
    return {
        "service": "lnurlcash-oracle",
        "version": __version__,
        "oraclePubkeyHex": ORACLE_PUBKEY_HEX,
        "trustModel": (
            "Single, named, curated oracle - not multi-oracle, not "
            "trustless. See DESIGN.md."
        ),
    }
