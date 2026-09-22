# lnurlcash-oracle

A single-oracle Discreet Log Contract (DLC) service - announces events
ahead of time, later attests to whichever outcome actually happened.
Settles bets locked with [lnurl-wallet](../lnurl-wallet)'s `betlocker`
addon (via the `dlc` addon's own oracle cryptography). See
[DESIGN.md](./DESIGN.md) for the full concept, trust model, and API.

Deliberately independent of lnurl-mint: nothing about a `ct1`/`cw1` note
references a specific oracle, and this service never touches a note, a
mint, or funds - it only ever signs outcome attestations.

**Not trustless.** A single, named, curated oracle. See DESIGN.md's own
"Trust model" section before relying on this for anything real.

## Requirements

- Python 3.12+
- `uv`

## Setup

```bash
make install
make gen-keypair          # prints ORACLE_SECRET_KEY_HEX=... - copy into .env
cp .env.example .env      # then fill in ORACLE_SECRET_KEY_HEX and ADMIN_API_KEY
make dev                  # http://localhost:8420, interactive docs at /docs
```

## Usage

```bash
# announce an event
curl -X POST http://localhost:8420/admin/events \
  -H "X-Admin-Key: $ADMIN_API_KEY" -H "Content-Type: application/json" \
  -d '{"eventId": "btc-100k-2026", "category": "btc-price",
       "outcomes": ["above", "below"], "maturityTime": 1790000000}'

# what a wallet's betlocker addon needs to lock a note against this event
curl http://localhost:8420/events/btc-100k-2026/announcement

# once maturityTime has passed, resolve it
curl -X POST http://localhost:8420/admin/events/btc-100k-2026/resolve \
  -H "X-Admin-Key: $ADMIN_API_KEY" -H "Content-Type: application/json" \
  -d '{"outcome": "above", "source": "coinbase BTC-USD index at maturity"}'

# what a wallet's betlocker addon needs to redeem
curl http://localhost:8420/events/btc-100k-2026/attestation
```

## Admin UI

A small self-contained page at `http://localhost:8420/admin` (no build
step, no external dependencies) wraps the `curl` calls above: announce an
event, list events, and resolve one (manually, or with one click via
"Auto-resolve" for `btc-price` events) without touching a terminal. The
admin key you enter there stays in that browser tab's `sessionStorage`
only, sent as `X-Admin-Key` to this same service - every action it takes
still goes through the same `/admin/*` API and the same server-side key
check as any other caller.

## Automatic btc-price events

On by default (`SCHEDULER_ENABLED=true` in `.env.example`) - harmless
until `PRICE_SOURCE_URL` points at a real, reachable price feed (a failed
fetch is just logged and retried next tick). Once it does,
`app/services/scheduler.py`'s background loop will, on its own:

- announce a fresh `btc-price` event every hour and every day
  (`btc-hourly-<hour>` / `btc-daily-<date>`), thresholded at whatever the
  real median price is at the moment it's created ("will BTC be above its
  own price right now, an hour/a day from now")
- auto-resolve any `btc-price` event once its `maturityTime` has passed,
  no admin action needed

Manually-curated events (any other category, or a `btc-price` event you
announce yourself) are completely unaffected - the scheduler only ever
touches the events it created the same automatic way. Set
`SCHEDULER_ENABLED=false` for a purely operator-curated deployment.

## Development

```bash
make test      # pytest - crypto vectors cross-checked against lnurl-wallet's
               # own dlc.ts, plus a full API lifecycle integration test
make lint      # ruff + mypy
make format    # ruff --fix
```

## Docker

```sh
mkdir -p data && touch data/oracle.db
docker run --restart always -d --name lnurlcash-oracle \
  --network host \
  --user "$(id -u):$(id -g)" \
  -e PORT=8420 \
  --env-file .env \
  -v "$(pwd)/data:/app/data" \
  lnurlcash/oracle                    # or: make run
```

The image runs as a non-root user; `--user` matches it to whichever host
user owns `data/` so it can write `oracle.db` and its sqlite journal/WAL
files (which must live in the *same directory*, not just the db file
itself).

`--network host` means no port remapping (`PORT` picks what the app
listens on) and no network isolation. Prefer real isolation? Drop
`--network host` and use `-p <host-port>:8420` instead. Front it with a
reverse proxy for TLS if it's reachable from the internet - this service
holds a real signing key, see DESIGN.md's own "Key management" section.

## Release

Pushing a `v*` tag (`git tag v1.2.0 && git push origin v1.2.0`) triggers
`.github/workflows/release.yml`, which:

- builds the image and pushes `lnurlcash/oracle` to Docker Hub,
  tagged `1.2.0`, `1.2`, `1`, and `latest`
- creates a GitHub Release for the tag (via `gh release create
  --generate-notes`), with notes auto-generated from the commits/PRs
  merged since the previous tag

Needs the repo secrets `DOCKERHUB_USERNAME` and `DOCKERHUB_TOKEN` (a
Docker Hub [access token](https://hub.docker.com/settings/security), not
the account password) configured on this repo before the first release.
