import asyncio
import contextlib
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from app import __version__
from app.config import settings
from app.database import async_session, init_db
from app.endpoints import admin, events
from app.identity import ORACLE_PUBKEY_HEX
from app.services.scheduler import scheduler_loop

STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    # one shared client for the app's whole lifetime (connection pooling,
    # not a fresh TCP/TLS handshake per price-source request) - read back
    # via app.endpoints.admin's own get_http_client dependency, and by the
    # scheduler task below
    async with httpx.AsyncClient() as client:
        app.state.http_client = client

        # on by default (Settings.SCHEDULER_ENABLED) - see scheduler.py's
        # own top comment for what this does and app/config.py's own
        # comment for why it's safe to leave on even before
        # PRICE_SOURCE_URL points at a real feed
        scheduler_task: asyncio.Task | None = None
        if settings.SCHEDULER_ENABLED:
            scheduler_task = asyncio.create_task(
                scheduler_loop(
                    async_session,
                    ORACLE_PUBKEY_HEX,
                    settings.ORACLE_SECRET_KEY_HEX,
                    client,
                    settings.PRICE_SOURCE_URL,
                    interval_seconds=settings.SCHEDULER_INTERVAL_SECONDS,
                )
            )
        try:
            yield
        finally:
            if scheduler_task is not None:
                scheduler_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await scheduler_task


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

# Public, read-only, cookie-free wire-protocol endpoints (events.router,
# GET / below) - meant to be fetched cross-origin by arbitrary third-party
# wallets, e.g. lnurl-wallet's own Betlocker/dlc oracleClient.ts running
# from a browser at a different origin. Same reasoning as lnurl-mint's own
# CORSMiddleware (server.py): nothing here reads a cookie, so a wide-open
# origin is safe.
#
# allow_methods deliberately stays GET-only, UNLIKE mint's own "*" - this
# service's only non-GET routes are /admin/* (real state-changing
# actions), which must stay same-origin (curl, server-to-server, or this
# service's own /admin page, always same-origin with itself). Restricting
# to GET here isn't just "we don't need more": a cross-origin POST to
# /admin/* would first need a CORS preflight, and Starlette's
# CORSMiddleware refuses to approve one for a method outside
# allow_methods - so this one line is what keeps a malicious page from
# ever getting a browser to even ATTEMPT a cross-origin
# /admin/events POST, on top of it needing the real X-Admin-Key regardless.
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["GET"], allow_headers=["*"]
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


# A static, self-contained admin page (app/static/admin.html) - a shell
# around the same /admin/* API a script could call; every privileged
# action it makes still requires the real X-Admin-Key header, checked
# server-side exactly as it is for any other caller. Serving the page
# itself needs no auth (same posture as GET /events or FastAPI's own
# /docs) - it reveals no secret on its own.
@app.get("/admin", include_in_schema=False)
async def admin_ui() -> FileResponse:
    return FileResponse(STATIC_DIR / "admin.html")
