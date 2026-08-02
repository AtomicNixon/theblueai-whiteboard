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
    pool = get_pool()
    async with pool.acquire() as conn:
        if include_archived:
            rows = await conn.fetch(
                "SELECT id, owner_did, title, status, created_at FROM canvases "
                "WHERE owner_did = $1 ORDER BY created_at DESC",
                did,
            )
        else:
            rows = await conn.fetch(
                "SELECT id, owner_did, title, status, created_at FROM canvases "
                "WHERE owner_did = $1 AND status = 'active' ORDER BY created_at DESC",
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
