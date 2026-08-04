"""Postgres access. Connection pool initialized at startup.

Schema (current-state only, no history):
  canvases        — id, owner_did, title, status, created_at
  canvas_elements — id, canvas_id, kind (text|mark), owner_did, data (JSONB),
                    created_at, updated_at
  canvas_members  — dids that have ever joined (for attribution / soft ACL later)

Element model (KISS):
  text  — owner_did set, mutable (only owner edits). data = {x,y,w,h,text,...}
  mark  — append-only, free-for-all. data = {points, stroke, ...} (strokes + shapes)
"""
from __future__ import annotations

import json
from typing import Any

import asyncpg

from .config import settings

_pool: asyncpg.Pool | None = None


async def init_pool() -> asyncpg.Pool:
    global _pool
    if _pool is not None:
        return _pool
    _pool = await asyncpg.create_pool(
        host=settings.pg_host,
        port=settings.pg_port,
        database=settings.pg_db,
        user=settings.pg_user,
        password=settings.pg_password,
        min_size=2,
        max_size=10,
    )
    async with _pool.acquire() as conn:
        await conn.execute(_SCHEMA)
    return _pool


def get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("DB pool not initialized — call init_pool first")
    return _pool


_SCHEMA = """
CREATE TABLE IF NOT EXISTS canvases (
    id          TEXT PRIMARY KEY,
    owner_did   TEXT NOT NULL,
    title       TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'active'
                CHECK(status IN ('active','archived')),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_canvases_owner ON canvases(owner_did);
CREATE INDEX IF NOT EXISTS idx_canvases_status ON canvases(status);

CREATE TABLE IF NOT EXISTS canvas_elements (
    id          TEXT PRIMARY KEY,
    canvas_id   TEXT NOT NULL REFERENCES canvases(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL CHECK(kind IN ('text','mark')),
    owner_did   TEXT NOT NULL,
    data        JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_elements_canvas ON canvas_elements(canvas_id);
CREATE INDEX IF NOT EXISTS idx_elements_kind ON canvas_elements(kind);

CREATE TABLE IF NOT EXISTS canvas_members (
    canvas_id   TEXT NOT NULL REFERENCES canvases(id) ON DELETE CASCADE,
    did         TEXT NOT NULL,
    handle      TEXT NOT NULL DEFAULT '',
    joined_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (canvas_id, did)
);

-- AT Protocol OAuth. See app/atproto_oauth.py for why this exists: bsky-mcp
-- tokens carry no account binding, so every user resolved as the same DID.

-- Our client signing key, published at /oauth/jwks.json. Kept in the database
-- rather than a file because the container has no persistent volume — a key
-- that changed on every deploy would invalidate in-flight authorizations.
CREATE TABLE IF NOT EXISTS oauth_client_key (
    id          TEXT PRIMARY KEY,
    jwk         TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Transient per-flow state: PKCE verifier, the flow's DPoP key, and the
-- identity we resolved before redirecting. Single-use — consumed by the
-- callback so an authorization code can't be replayed.
CREATE TABLE IF NOT EXISTS oauth_auth_request (
    state                 TEXT PRIMARY KEY,
    authserver_iss        TEXT NOT NULL,
    did                   TEXT NOT NULL,
    handle                TEXT NOT NULL,
    pds_url               TEXT NOT NULL,
    pkce_verifier         TEXT NOT NULL,
    dpop_private_jwk      TEXT NOT NULL,
    dpop_authserver_nonce TEXT NOT NULL DEFAULT '',
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Images. Excalidraw keeps binary in a separate BinaryFiles map keyed by
-- fileId, not on the element, so we mirror that rather than stuffing a data
-- URI inside element JSON.
--
-- The browser downscales and re-encodes to JPEG before upload, so what lands
-- here is bounded by construction: a 12 MP phone photo and a screenshot cost
-- roughly the same. That bound is the whole point — an unbounded blob store on
-- a 2 GB box shared with a PDS and a Postgres is how you lose a weekend.
CREATE TABLE IF NOT EXISTS canvas_files (
    canvas_id   TEXT NOT NULL REFERENCES canvases(id) ON DELETE CASCADE,
    file_id     TEXT NOT NULL,
    mime_type   TEXT NOT NULL DEFAULT 'image/jpeg',
    data_url    TEXT NOT NULL,
    owner_did   TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (canvas_id, file_id)
);
CREATE INDEX IF NOT EXISTS idx_canvas_files_canvas ON canvas_files(canvas_id);

-- Whiteboard sessions. We store a SHA-256 of the token, never the token
-- itself, so a database leak doesn't hand over live sessions.
CREATE TABLE IF NOT EXISTS wb_session (
    token_hash  TEXT PRIMARY KEY,
    did         TEXT NOT NULL,
    handle      TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at  TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_wb_session_did ON wb_session(did);
CREATE INDEX IF NOT EXISTS idx_wb_session_expires ON wb_session(expires_at);
"""


# --- Canvas CRUD ---


async def create_canvas(canvas_id: str, owner_did: str, title: str) -> dict[str, Any]:
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO canvases (id, owner_did, title) VALUES ($1, $2, $3) "
            "RETURNING id, owner_did, title, status, created_at",
            canvas_id, owner_did, title,
        )
    return dict(row)


async def get_canvas(canvas_id: str) -> dict[str, Any] | None:
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, owner_did, title, status, created_at FROM canvases WHERE id = $1",
            canvas_id,
        )
    return dict(row) if row else None


async def list_canvases_for(did: str, include_archived: bool = False) -> list[dict[str, Any]]:
    """Canvases you own OR have joined.

    Owner-only would mean a canvas someone invites you to never appears in your
    list — you'd have to already know its id, which rather defeats a shared
    canvas. Membership is recorded on first visit (upsert_member), so opening a
    canvas once puts it in your list from then on.
    """
    status_clause = "" if include_archived else "AND c.status = 'active' "
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT DISTINCT c.id, c.owner_did, c.title, c.status, c.created_at "
            "FROM canvases c "
            "LEFT JOIN canvas_members m ON m.canvas_id = c.id AND m.did = $1 "
            "WHERE (c.owner_did = $1 OR m.did IS NOT NULL) "
            f"{status_clause}"
            "ORDER BY c.created_at DESC",
            did,
        )
    return [dict(r) for r in rows]


async def set_canvas_status(canvas_id: str, status: str) -> bool:
    pool = get_pool()
    async with pool.acquire() as conn:
        res = await conn.execute(
            "UPDATE canvases SET status = $1 WHERE id = $2", status, canvas_id
        )
    return res.endswith("1")


# --- Elements ---


async def add_element(
    element_id: str, canvas_id: str, kind: str, owner_did: str, data: dict[str, Any]
) -> dict[str, Any]:
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO canvas_elements (id, canvas_id, kind, owner_did, data) "
            "VALUES ($1, $2, $3, $4, $5) "
            "RETURNING id, canvas_id, kind, owner_did, data, created_at, updated_at",
            element_id, canvas_id, kind, owner_did, json.dumps(data),
        )
    return dict(row, data=json.loads(row["data"]))


async def get_element(element_id: str) -> dict[str, Any] | None:
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, canvas_id, kind, owner_did, data, created_at, updated_at "
            "FROM canvas_elements WHERE id = $1",
            element_id,
        )
    return dict(row, data=json.loads(row["data"])) if row else None


async def update_element(
    element_id: str, owner_did: str, data: dict[str, Any]
) -> dict[str, Any] | None:
    """Update an element. Text: only the owner. Mark: free-for-all (chaos welcome).

    Mirrors delete_element's rule exactly — a mark you can erase is a mark you
    can move.
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "UPDATE canvas_elements SET data = $1, updated_at = now() "
            "WHERE id = $2 AND (kind = 'mark' OR owner_did = $3) "
            "RETURNING id, canvas_id, kind, owner_did, data, created_at, updated_at",
            json.dumps(data), element_id, owner_did,
        )
    return dict(row, data=json.loads(row["data"])) if row else None


async def delete_element(element_id: str, owner_did: str) -> bool:
    """Delete an element. Text: only owner. Mark: free-for-all (chaos welcome)."""
    pool = get_pool()
    async with pool.acquire() as conn:
        # Allow if caller owns it, OR if it's a mark (anyone can erase).
        res = await conn.execute(
            "DELETE FROM canvas_elements WHERE id = $1 AND (owner_did = $2 OR kind = 'mark')",
            element_id, owner_did,
        )
    return res.endswith("1")


async def get_canvas_elements(canvas_id: str) -> list[dict[str, Any]]:
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, canvas_id, kind, owner_did, data, created_at, updated_at "
            "FROM canvas_elements WHERE canvas_id = $1 ORDER BY created_at ASC",
            canvas_id,
        )
    return [dict(r, data=json.loads(r["data"])) for r in rows]


async def get_canvas_snapshot(canvas_id: str) -> dict[str, Any] | None:
    """Full current state — used on (re)connect before switching to live stream."""
    canvas = await get_canvas(canvas_id)
    if canvas is None:
        return None
    elements = await get_canvas_elements(canvas_id)
    return {"canvas": canvas, "elements": elements}


# --- Members ---


async def upsert_member(canvas_id: str, did: str, handle: str) -> None:
    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO canvas_members (canvas_id, did, handle, last_seen) "
            "VALUES ($1, $2, $3, now()) "
            "ON CONFLICT (canvas_id, did) DO UPDATE "
            "SET handle = EXCLUDED.handle, last_seen = now()",
            canvas_id, did, handle,
        )


async def list_members(canvas_id: str) -> list[dict[str, Any]]:
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT did, handle, joined_at, last_seen FROM canvas_members "
            "WHERE canvas_id = $1 ORDER BY last_seen DESC",
            canvas_id,
        )
    return [dict(r) for r in rows]


# --- OAuth: client key, flow state, sessions ---


async def get_or_create_client_jwk(generate: Any) -> dict[str, Any]:
    """Fetch the client signing key, generating it on first use.

    `generate` is a zero-arg callable returning a private JWK dict, injected so
    this module stays free of crypto imports. Racing callers are fine: the
    INSERT is ON CONFLICT DO NOTHING and we re-read the winner.
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT jwk FROM oauth_client_key WHERE id = 'default'")
        if row:
            return json.loads(row["jwk"])
        await conn.execute(
            "INSERT INTO oauth_client_key (id, jwk) VALUES ('default', $1) "
            "ON CONFLICT (id) DO NOTHING",
            json.dumps(generate()),
        )
        row = await conn.fetchrow("SELECT jwk FROM oauth_client_key WHERE id = 'default'")
    return json.loads(row["jwk"])


async def save_auth_request(req: dict[str, Any]) -> None:
    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO oauth_auth_request "
            "(state, authserver_iss, did, handle, pds_url, pkce_verifier, "
            " dpop_private_jwk, dpop_authserver_nonce) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7,$8)",
            req["state"], req["authserver_iss"], req["did"], req["handle"],
            req["pds_url"], req["pkce_verifier"], req["dpop_private_jwk"],
            req.get("dpop_authserver_nonce", ""),
        )


async def take_auth_request(state: str) -> dict[str, Any] | None:
    """Fetch and delete in one statement — an auth request is single-use.

    DELETE ... RETURNING is atomic, so two concurrent callbacks replaying the
    same code cannot both succeed.
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "DELETE FROM oauth_auth_request WHERE state = $1 RETURNING *", state
        )
    return dict(row) if row else None


async def prune_auth_requests(max_age_seconds: int = 600) -> int:
    """Drop abandoned flows. Returns how many were removed."""
    pool = get_pool()
    async with pool.acquire() as conn:
        res = await conn.execute(
            "DELETE FROM oauth_auth_request "
            f"WHERE created_at < now() - interval '{int(max_age_seconds)} seconds'"
        )
    return int(res.rsplit(" ", 1)[-1] or 0)


async def create_session(token_hash: str, did: str, handle: str, ttl_seconds: int) -> None:
    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO wb_session (token_hash, did, handle, expires_at) "
            f"VALUES ($1, $2, $3, now() + interval '{int(ttl_seconds)} seconds') "
            "ON CONFLICT (token_hash) DO NOTHING",
            token_hash, did, handle,
        )


async def get_session(token_hash: str) -> dict[str, str] | None:
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT did, handle FROM wb_session "
            "WHERE token_hash = $1 AND expires_at > now()",
            token_hash,
        )
    return {"did": row["did"], "handle": row["handle"]} if row else None


async def delete_session(token_hash: str) -> bool:
    pool = get_pool()
    async with pool.acquire() as conn:
        res = await conn.execute("DELETE FROM wb_session WHERE token_hash = $1", token_hash)
    return res.endswith("1")


async def prune_sessions() -> int:
    pool = get_pool()
    async with pool.acquire() as conn:
        res = await conn.execute("DELETE FROM wb_session WHERE expires_at <= now()")
    return int(res.rsplit(" ", 1)[-1] or 0)


# --- Images ---


async def put_file(canvas_id: str, file_id: str, mime_type: str,
                   data_url: str, owner_did: str) -> None:
    """Store an image for a canvas. Idempotent — the same paste may arrive twice."""
    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO canvas_files (canvas_id, file_id, mime_type, data_url, owner_did) "
            "VALUES ($1, $2, $3, $4, $5) "
            "ON CONFLICT (canvas_id, file_id) DO NOTHING",
            canvas_id, file_id, mime_type, data_url, owner_did,
        )


async def get_files(canvas_id: str) -> list[dict[str, Any]]:
    """Every image on a canvas, in Excalidraw's BinaryFiles shape."""
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT file_id, mime_type, data_url FROM canvas_files WHERE canvas_id = $1",
            canvas_id,
        )
    return [{"id": r["file_id"], "mimeType": r["mime_type"], "dataURL": r["data_url"]}
            for r in rows]


async def canvas_files_bytes(canvas_id: str) -> int:
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT COALESCE(SUM(LENGTH(data_url)), 0) AS n FROM canvas_files WHERE canvas_id = $1",
            canvas_id,
        )
    return int(row["n"])


async def delete_orphan_files(canvas_id: str) -> int:
    """Drop images no element references any more.

    Deleting an image element leaves its file behind; without this a canvas
    would accumulate the bytes of every picture ever pasted into it.
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        res = await conn.execute(
            "DELETE FROM canvas_files f WHERE f.canvas_id = $1 AND NOT EXISTS ("
            "  SELECT 1 FROM canvas_elements e"
            "  WHERE e.canvas_id = f.canvas_id AND e.data->>'fileId' = f.file_id)",
            canvas_id,
        )
    return int(res.rsplit(" ", 1)[-1] or 0)
