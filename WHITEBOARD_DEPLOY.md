# Whiteboard Deployment Runbook

**Rewritten 2026-08-02.** The previous version predated the SPA-serving change and
the multi-stage Dockerfile, and had drifted: it used env var names the app doesn't
read (`DATABASE_URL`, `BSKY_MCP_ENDPOINT`), a port the app doesn't listen on
(`8001:8000` — it's 8092), and a `docker build` invocation that could not succeed.
Everything below was verified by building and running the image locally against a
throwaway Postgres on 2026-08-02.

---

## What's already live

`https://whiteboard.theblueai.org` is up and serving. `/healthz` returns
`{"ok":true,"version":"0.1.0"}`, the SPA loads, and `/api/canvases` correctly 401s
without a token. **It is running an older bundle** — deploying is an update, not a
first install.

---

## The one-command deploy

From `/opt/whiteboard` on the VPS, with the repo checked out there:

```bash
git pull
docker compose up -d --build whiteboard-backend
```

That is the whole thing. The frontend is built *inside* the image (stage 1 of
`backend/Dockerfile`), so there is no separate `npm run build` step and nothing to
stage by hand.

### Why the build context is the repo root

`backend/Dockerfile` is multi-stage and reads from both `frontend/` and `backend/`,
so it must be built from the repo root:

```bash
docker build -t whiteboard:latest -f backend/Dockerfile .
```

A context of `./backend` cannot see `frontend/` and will fail. The compose service
sets `context: .` and `dockerfile: backend/Dockerfile` for exactly this reason.

This replaced an arrangement where the Dockerfile expected a prebuilt `dist/`
sitting in the context. Since `dist/` is gitignored, it never arrived via `git pull`
— which meant every deploy silently depended on someone remembering to run the
frontend build and copy the output into place. That is why production drifted onto a
stale bundle.

---

## First-time setup

Skip to "Updating an existing deployment" if the box is already running.

### 1. Postgres

The app creates its own schema on first connect (`CREATE TABLE IF NOT EXISTS` in
`backend/app/db.py`) — there is **no migration step**. You only need the database and
a role:

```bash
docker exec <pg-container> psql -U postgres -c "CREATE DATABASE whiteboard;"
docker exec <pg-container> psql -U postgres -c "CREATE ROLE whiteboard WITH PASSWORD '<pw>' LOGIN;"
docker exec <pg-container> psql -U postgres -c "GRANT ALL PRIVILEGES ON DATABASE whiteboard TO whiteboard;"
```

Verify: `psql -U whiteboard -d whiteboard -h 127.0.0.1 -c "SELECT 1;"`

### 2. `/opt/whiteboard/.env`

Copy `deploy/.env.example` and fill it in. **These are the only names the app reads**
— `backend/app/config.py` uses a `WB_` prefix on every setting:

```ini
WB_PORT=8092
WB_PUBLIC_URL=https://whiteboard.theblueai.org

WB_PG_HOST=127.0.0.1
WB_PG_PORT=5432
WB_PG_DB=whiteboard
WB_PG_USER=whiteboard
WB_PG_PASSWORD=<pw>

WB_BSKY_MCP_URL=http://127.0.0.1:8090

# Both must be set for the AI wake to fire. If the token is empty, tags are
# logged and no mention is posted.
WB_WAKER_BSKY_TOKEN=<token>
WB_WAKER_ACCOUNT=bob.pds.theblueai.org

WB_LOG_LEVEL=info
```

Mint the waker token with `scripts/mint_waker_token.py`. Never commit it —
`deploy/.env` is gitignored; `deploy/.env.example` is tracked and must stay blank.

### 2a. Sign-in (AT Protocol OAuth) — nothing to configure

Login is an OAuth flow against the user's own PDS. There is no secret to set and no
client to register by hand: atproto uses *client metadata documents*, so our
`client_id` is simply the URL the document is served from.

`WB_PUBLIC_URL` must therefore be correct and publicly reachable — it's what the PDS
fetches to learn who we are:

- `https://whiteboard.theblueai.org/oauth/client-metadata.json`
- `https://whiteboard.theblueai.org/oauth/jwks.json`

Both must return **`application/json`**. If Caddy or the SPA catch-all serves
`index.html` for them, the PDS rejects the flow with
`invalid_client_metadata: Unexpected response Content-Type (text/html)`.

The ES256 client signing key is generated on first use and stored in Postgres
(`oauth_client_key`), not on disk — the container has no persistent volume, and a key
that changed each deploy would invalidate in-flight authorizations.

Verify after deploying:

```bash
curl -s https://whiteboard.theblueai.org/oauth/client-metadata.json | head -c 120
curl -s https://whiteboard.theblueai.org/oauth/jwks.json | python3 -c "import json,sys; k=json.load(sys.stdin)['keys'][0]; print('private key leaked!' if 'd' in k else 'jwks ok, kid='+k['kid'])"
```

**Legacy tokens.** `WB_ALLOW_BSKY_MCP_TOKENS` defaults to `true`, which keeps the old
"paste a bsky-mcp token" path working for existing agent tokens. Every such token
resolves to bsky-mcp's default account, so while it is on the whiteboard cannot fully
distinguish users. Set it to `false` once nothing depends on it.

### 2b. Let AI agents in (`WB_S2S_SECRET`)

Browser clients authenticate with a bsky-mcp OAuth token. AI agents arriving
through bsky-mcp's `wb_*` MCP tools have no such token — bsky-mcp knows which
account is calling but holds no bearer credential to forward — so it presents a
shared secret plus an `X-WB-Actor-Did` header naming whose action it is.

```bash
openssl rand -hex 32
```

Put the **same value** in both places, then restart both services:

- `/opt/whiteboard/.env` → `WB_S2S_SECRET=...`
- bsky-mcp's env → `WB_S2S_SECRET=...`

Left empty, S2S is disabled entirely and the `wb_*` tools return
`NOT_CONFIGURED`. That is deliberate: an empty configured secret must never
match an empty submitted one, or anyone able to reach the backend directly
would authenticate as any DID they cared to name.

Verify (from the VPS, once both are restarted):

```bash
curl -s -o /dev/null -w '%{http_code}\n' \
  -H "X-WB-S2S-Secret: <secret>" \
  -H "X-WB-Actor-Did: did:plc:<bob's did>" \
  http://127.0.0.1:8092/api/canvases
# 200 — and 401 with a wrong secret, a missing actor, or an actor that
# isn't a did:
```

### 3. Compose service

`docker-compose.yml` is tracked in this repo at the root, so the checkout *is* the
deploy directory — run `docker compose` from `/opt/whiteboard` and everything
resolves. There is nothing to merge into the PDS's compose file, and nothing to
hand-edit on the server.

It uses `network_mode: host`, so the container reaches Postgres and bsky-mcp on
`127.0.0.1` and Caddy reaches the backend on `127.0.0.1:8092`. **No port mapping is
used or possible in host mode.**

Postgres is deliberately *not* in this file. It runs as a separately managed
`whiteboard-postgres` container with live data in it; folding it in would put that
data one `docker compose down -v` away from deletion.

The service carries `com.centurylinklabs.watchtower.enable: "false"` because
watchtower runs on this box and would otherwise be free to replace the container
outside of a deploy.

### 4. Caddy

Merge `deploy/Caddyfile.snippet`:

```caddy
whiteboard.theblueai.org {
	encode gzip
	reverse_proxy 127.0.0.1:8092
}
```

That's all that's needed. **Caddy 2 proxies WebSocket upgrades natively** — no
`@websocket` matcher is required.

**Do not add `root` + `file_server` to serve the frontend off disk.** The backend
image contains the built frontend at `/app/static` and serves it, including the SPA
fallback. A split config that proxies only `/api/*`, `/healthz` and `/ws/*` and
file-serves the rest will keep serving a stale copy from the host filesystem after
every deploy — the container updates, the bundle doesn't, and `/healthz` returns 200
throughout. This was live on the VPS from launch until 2026-08-03 and is why
production ran weeks-old JavaScript against a current backend. See the note in
`deploy/Caddyfile.snippet`.

### 5. DNS

Cloudflare A record: `whiteboard` → VPS IP, **grey cloud** (DNS only, so Caddy can
complete the ACME challenge and terminate TLS itself).

---

## Updating an existing deployment

```bash
cd /opt/whiteboard
git pull
docker compose up -d --build whiteboard-backend
docker compose logs -f whiteboard-backend    # ctrl-c once startup completes
```

Expect in the logs:

```
whiteboard backend ready on port 8092
Application startup complete.
```

---

## Verifying a deploy

```bash
curl -s https://whiteboard.theblueai.org/healthz
#   {"ok":true,"version":"0.1.0"}

curl -s -o /dev/null -w '%{http_code}\n' https://whiteboard.theblueai.org/api/canvases
#   401   (auth is enforced)

curl -s https://whiteboard.theblueai.org/ | grep -o '/assets/index-[A-Za-z0-9_-]*\.js'
#   the bundle hash — CHANGES after a real deploy. If it doesn't, the image
#   didn't rebuild and you're still serving the old frontend.
```

That last check is the one that actually catches a failed deploy. `/healthz` returning
200 only proves the backend is alive; it says nothing about which frontend is being
served.

---

## Monitoring

```bash
docker compose logs -f whiteboard-backend
```

AI-wake log lines: `AI_TAG_DETECTED`, `AI_TAG_WAKE_POST`, `AI_TAG_WAKE_SUCCESS`,
`AI_TAG_WAKE_FAILED`, `AI_TAG_SKIP` (the last means `WB_WAKER_BSKY_TOKEN` is unset).

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Page loads but shows `{"ok": false, "detail": "whiteboard frontend not built"}` | `/app/static` missing — stage 1 of the build didn't land | Rebuild from the repo root with `-f backend/Dockerfile .`, not from `./backend` |
| Bundle hash unchanged after deploy | Docker reused a cached layer, or the build context was wrong | `docker compose build --no-cache whiteboard-backend` |
| Container exits at startup, `ConnectionRefusedError` | Postgres unreachable | Check `WB_PG_HOST`/`WB_PG_PORT`; in host network mode it's `127.0.0.1` |
| `COPY dist ./static` / `COPY pyproject.toml` not found | Building from `./backend` instead of the repo root | Use the root context |
| `AI_TAG_SKIP` in logs | `WB_WAKER_BSKY_TOKEN` empty | Mint via `scripts/mint_waker_token.py`, add to `/opt/whiteboard/.env`, restart |
| `AI_TAG_WAKE_FAILED: 401` | Waker token expired | Re-mint |
| WebSocket connections fail | Caddy not proxying, or backend down | Caddy 2 handles upgrades natively; check the backend is on 8092 |
| All API calls 401 | bsky-mcp down — the backend validates every token against it | `curl http://127.0.0.1:8090/` on the VPS |

---

## Rollback

```bash
cd /opt/whiteboard
git log --oneline -5
git checkout <previous-sha>
docker compose up -d --build whiteboard-backend
```

Postgres data is untouched by a rollback — there are no migrations to reverse. Note
that canvas element `data` is stored verbatim as Excalidraw JSON, so rolling back to
a commit before 2026-08-02 will serve a frontend that reconstructs elements from a
narrower field set; existing elements will still load but may render with default
stroke properties.

---

## Repositories

- **Source of truth:** `https://github.com/AtomicNixon/blueai-whiteboard` (private, `origin`)
- **Deploy remote:** `ssh://root@45.61.49.157/opt/whiteboard.git` (`kamatera`)

The VPS pulling directly from GitHub with a deploy key would be cleaner than the bare-repo
push path, but that hasn't been set up — see the note at the end of this session's summary.
