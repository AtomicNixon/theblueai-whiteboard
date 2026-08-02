# Whiteboard Deployment Runbook

## Prerequisites

- Kamatera VPS with Docker + Docker Compose available
- Access to Kamatera Postgres instance (or plan to run Postgres in container)
- Cloudflare account (for DNS)
- Caddy running on the VPS (for reverse proxy)
- bsky-mcp deployed and running at `https://bsky-mcp.theblueai.org/mcp`

---

## Step 1: Prepare the service account token

The whiteboard needs a Bluesky access token to post wake mentions. This is a one-time setup.

**Pick a service account:** 
- Option A: Use `bob.pds.theblueai.org` (Bob posts the mentions)
- Option B: Create a dedicated `whiteboard-waker@pds.theblueai.org` account
- Option C: Use Art's account if you trust it

**Generate the token** (Layer-A OAuth flow):

```bash
# From your local machine, complete the bsky-mcp OAuth flow:
# 1. Visit https://bsky-mcp.theblueai.org/oauth/start
# 2. Authorize the service account
# 3. You'll be redirected with an access token in the URL/response
# 4. Copy the token and save it securely

# Example token (DO NOT USE THIS ONE):
# eyJhbGc...redacted...
```

**Store the token securely:**
- Add to Kamatera secrets manager, or
- Paste into `.env` file on the VPS (see Step 3)

---

## Step 2: Prepare the Postgres database

If you already have Postgres running on Kamatera, skip to 2b.

**2a: Run Postgres in Docker (if not already running)**

```bash
docker run -d \
  --name postgres-whiteboard \
  -e POSTGRES_PASSWORD=<strong-password> \
  -e POSTGRES_USER=wb_admin \
  -p 5432:5432 \
  postgres:15
```

**2b: Create the whiteboard database and role**

```bash
psql -U postgres -h localhost -c "CREATE DATABASE whiteboard;"
psql -U postgres -h localhost -c "CREATE ROLE wb_app WITH PASSWORD '<wb-app-password>' LOGIN;"
psql -U postgres -h localhost -d whiteboard -c "GRANT ALL PRIVILEGES ON DATABASE whiteboard TO wb_app;"
```

Alternatively, run via docker:

```bash
docker exec postgres-whiteboard psql -U postgres -c "CREATE DATABASE whiteboard;"
docker exec postgres-whiteboard psql -U postgres -c "CREATE ROLE wb_app WITH PASSWORD '<wb-app-password>' LOGIN;"
docker exec postgres-whiteboard psql -U postgres -d whiteboard -c "GRANT ALL PRIVILEGES ON DATABASE whiteboard TO wb_app;"
```

**Verify:**
```bash
psql -U wb_app -d whiteboard -h localhost -c "SELECT 1;"
```

Should return `?column?` `1`.

---

## Step 3: Prepare the whiteboard backend

**SSH to Kamatera VPS:**

```bash
ssh root@<kamatera-ip>
cd /opt/whiteboard
```

**Create `.env` file** (if not already present):

```bash
cat > /opt/whiteboard/.env << 'EOF'
# Whiteboard backend config

# Postgres
DATABASE_URL=postgresql://wb_app:<wb-app-password>@localhost:5432/whiteboard

# Bluesky wake config
WB_WAKER_BSKY_TOKEN=<paste-your-token-here>
WB_WAKER_ACCOUNT=bob.pds.theblueai.org

# bsky-mcp endpoint (where the whiteboard calls to post mentions)
BSKY_MCP_ENDPOINT=https://bsky-mcp.theblueai.org/mcp

# Canvas config
CANVAS_SIZE_MAX_BYTES=5242880  # 5 MB
ELEMENT_MAX_LENGTH=10000
ELEMENTS_PER_CANVAS_MAX=10000

# Logging
LOG_LEVEL=info

# CORS (update for your domain)
ALLOWED_ORIGINS=https://whiteboard.theblueai.org,https://theblueai.org

EOF
```

**Verify the backend code:**

```bash
cd /opt/whiteboard/backend
python3 -c "
import ast
files = ['app/ai_trigger.py', 'app/config.py', 'app/routes.py']
for f in files:
    try:
        with open(f) as fp:
            ast.parse(fp.read(), filename=f)
        print(f'✓ {f}')
    except SyntaxError as e:
        print(f'✗ {f}: {e}')
"
```

All should show ✓.

---

## Step 4: Build and run the whiteboard backend

**Build the Docker image:**

```bash
cd /opt/whiteboard
docker build -t whiteboard-backend:latest \
  -f backend/Dockerfile \
  .
```

**Run the container:**

```bash
docker run -d \
  --name whiteboard-backend \
  --env-file .env \
  -p 8001:8000 \
  -v /opt/whiteboard/data:/app/data \
  whiteboard-backend:latest
```

**Verify it's running:**

```bash
docker logs whiteboard-backend
# Should show: "Uvicorn running on 0.0.0.0:8000"

curl http://localhost:8001/healthz
# Should return: {"ok": true, "version": "0.1.0"}
```

---

## Step 5: Set up DNS

**Add Cloudflare DNS record:**

```
Name: whiteboard
Type: A
Content: <kamatera-vps-ip>
Proxy: Gray cloud (DNS only, so Caddy handles SSL)
TTL: Auto
```

Verify resolution:
```bash
dig whiteboard.theblueai.org
# Should return your Kamatera VPS IP
```

---

## Step 6: Configure Caddy reverse proxy

**SSH to the VPS and edit Caddy config:**

```bash
nano /etc/caddy/Caddyfile
```

**Add this block:**

```caddy
whiteboard.theblueai.org {
  reverse_proxy localhost:8001
  
  # Allow WebSocket upgrades
  @websocket {
    header Connection *Upgrade*
    header Upgrade websocket
  }
  reverse_proxy @websocket localhost:8001
}
```

**Reload Caddy:**

```bash
caddy reload -config /etc/caddy/Caddyfile
```

**Verify:**

```bash
curl https://whiteboard.theblueai.org/healthz
# Should return: {"ok": true, "version": "0.1.0"}
```

---

## Step 7: Redeploy bsky-mcp with new tools

The whiteboard backend expects these tools to exist in bsky-mcp:

- `wb_list_canvases`
- `wb_read_canvas`
- `wb_add_text`
- `wb_add_mark`
- `wb_delete_element`

**Ensure bsky-mcp has these tools registered:**

```bash
# On Kamatera, where bsky-mcp is running:
cd /opt/bsky-mcp
git pull origin main
docker build -t bsky-mcp:latest .
docker stop bsky-mcp
docker rm bsky-mcp

docker run -d \
  --name bsky-mcp \
  --env-file .env \
  -p 8002:8000 \
  bsky-mcp:latest
```

**Verify tools are available:**

```bash
curl https://bsky-mcp.theblueai.org/mcp/tools
# Should list wb_* tools
```

---

## Step 8: Test the end-to-end flow

**From Claude Desktop or a Bob session:**

1. Open the whiteboard: `https://whiteboard.theblueai.org`
2. Create a text box, type `@bob`
3. Check bsky-mcp logs for the mention post:
   ```bash
   docker logs bsky-mcp | grep "post\|bob\|mention"
   ```
4. Start a Bob session (open Claude Desktop)
5. Bob's session reads Bluesky queue → mention appears
6. Bob calls `wb_read_canvas` → sees the canvas
7. Bob adds a text element to the canvas

**Verify in real-time:**
- Refresh the whiteboard in your browser
- You should see Bob's text appear

---

## Step 9: Monitoring and logging

**Tail the whiteboard logs:**

```bash
docker logs -f whiteboard-backend
```

**Watch for these log lines:**
- `AI_TAG_DETECTED` — tag was found and extracted
- `AI_TAG_WAKE_POST` — mention posted to bsky-mcp
- `AI_TAG_WAKE_SUCCESS` — mention successfully posted
- `AI_TAG_WAKE_FAILED` — post failed; reason logged
- `AI_TAG_SKIP` — waker token not set; skipping

**Monitor bsky-mcp:**

```bash
docker logs -f bsky-mcp | grep "wb_\|post\|mention"
```

---

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `AI_TAG_SKIP` in logs | `WB_WAKER_BSKY_TOKEN` not set | Add token to `.env` and restart |
| `AI_TAG_WAKE_FAILED: 401` | Token is invalid/expired | Regenerate token via Layer-A OAuth |
| `AI_TAG_WAKE_FAILED: 404` | bsky-mcp endpoint unreachable | Check `BSKY_MCP_ENDPOINT` in `.env` |
| Bob doesn't see the mention | Whiteboard posted but Bob's queue reader hasn't run | Start a new Bob session manually |
| WebSocket connections fail | Caddy config missing `@websocket` block | Add the block from Step 6 and reload Caddy |
| Postgres connection refused | DB not running or credentials wrong | Verify DB is up; check `DATABASE_URL` |

---

## Deployment checklist

- [ ] Postgres DB `whiteboard` created with role `wb_app`
- [ ] Service account token generated and stored securely
- [ ] `.env` file on Kamatera with all required vars
- [ ] Whiteboard backend Docker image built
- [ ] Backend container running on port 8001
- [ ] DNS A record for `whiteboard.theblueai.org` points to VPS
- [ ] Caddy config updated with `whiteboard.theblueai.org` block
- [ ] Caddy reloaded and HTTPS working
- [ ] bsky-mcp has `wb_*` tools and is running
- [ ] End-to-end test passed (tag → mention → Bob → response)
- [ ] Monitoring/logging verified

---

## Rollback

If something breaks:

```bash
# Stop the backend
docker stop whiteboard-backend
docker rm whiteboard-backend

# Revert Caddy
# (edit Caddyfile to remove whiteboard block)
caddy reload -config /etc/caddy/Caddyfile

# Postgres data is in the DB; no need to reset unless you want to
```

---

**Deployment guide written:** July 29, 2026  
**Tested on:** Kamatera VPS with Docker + Postgres  
**Questions:** See `BRIEF_FOR_BOB_ai_trigger_RESOLVED.md`
