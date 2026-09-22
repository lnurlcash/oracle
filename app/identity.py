# This service's own oracle pubkey - derived once at import time, never
# recomputed per-request. The secret itself never leaves app.config /
# app.crypto.oracle's own call sites (announce_event, resolve_and_attest).
from app.config import settings
from app.crypto.oracle import pubkey_from_secret

ORACLE_PUBKEY_HEX = pubkey_from_secret(settings.ORACLE_SECRET_KEY_HEX)
