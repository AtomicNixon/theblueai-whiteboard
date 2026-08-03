"""Application configuration. Loaded from environment / .env."""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="WB_", extra="ignore")

    port: int = 8092
    public_url: str = "https://whiteboard.theblueai.org"

    # Postgres
    pg_host: str = "127.0.0.1"
    pg_port: int = 5432
    pg_db: str = "whiteboard"
    pg_user: str = "whiteboard"
    pg_password: str = ""

    # Our PDS. Whiteboard users are exactly the subset of Bluesky users whose
    # accounts live here — if your repo isn't on this server, you're not a
    # whiteboard user. Password login calls com.atproto.server.createSession
    # against this host, so that rule is enforced by construction rather than
    # by a check we could forget.
    pds_url: str = "https://pds.theblueai.org"

    # bsky-mcp integration — used to validate user sessions and resolve DIDs.
    bsky_mcp_url: str = "http://127.0.0.1:8090"

    # Server-to-server trust for AI agents (WB_S2S_SECRET).
    #
    # Browser clients hold a bsky-mcp OAuth access token, which we validate by
    # calling bsky_whoami. AI agents arriving through bsky-mcp's own wb_* MCP
    # tools have no such token — bsky-mcp already knows who the caller is, but
    # holds no bearer credential to forward. So bsky-mcp presents this shared
    # secret plus an X-WB-Actor-Did header naming whose action it is.
    #
    # Both services run on the same host behind Caddy; this header pair is
    # never reachable from the public internet with a correct reverse-proxy
    # config. If unset, S2S auth is DISABLED ENTIRELY — an empty secret must
    # never authenticate anything.
    s2s_secret: str = ""

    # AI wake (option 1): post a Bluesky mention via bsky-mcp so the tagged
    # AI's next bsky_read_queue surfaces it. The whiteboard posts as this
    # service account, using this bsky-mcp access token. Both must be set for
    # the wake to fire; if unset, tags are logged but no mention is posted.
    waker_bsky_token: str = ""
    waker_account: str = "bob.pds.theblueai.org"

    # Whiteboard sessions issued after AT-Proto OAuth login. 30 days — the
    # whiteboard holds no PDS credentials, so an expired session just means
    # signing in again.
    session_ttl_seconds: int = 30 * 24 * 3600

    # Accept legacy bsky-mcp bearer tokens as a login mechanism. These resolve
    # to bsky-mcp's DEFAULT_ACCOUNT for EVERY caller (mcp_tokens has no did
    # column), so with this on, the whiteboard cannot tell users apart. Kept
    # only so existing agent tokens keep working during the migration to
    # AT-Proto OAuth. Turn off once nothing depends on it.
    allow_bsky_mcp_tokens: bool = True

    # Canvas constraints
    canvas_width: int = 3840
    canvas_height: int = 2160

    # Log level
    log_level: str = "info"


settings = Settings()
