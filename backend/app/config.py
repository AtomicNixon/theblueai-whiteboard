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

    # bsky-mcp integration — used to validate user sessions and resolve DIDs.
    bsky_mcp_url: str = "http://127.0.0.1:8090"

    # AI wake (option 1): post a Bluesky mention via bsky-mcp so the tagged
    # AI's next bsky_read_queue surfaces it. The whiteboard posts as this
    # service account, using this bsky-mcp access token. Both must be set for
    # the wake to fire; if unset, tags are logged but no mention is posted.
    waker_bsky_token: str = ""
    waker_account: str = "bob.pds.theblueai.org"

    # Canvas constraints
    canvas_width: int = 3840
    canvas_height: int = 2160

    # Log level
    log_level: str = "info"


settings = Settings()
