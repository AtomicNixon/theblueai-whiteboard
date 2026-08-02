"""Shared row -> JSON serializers for HTTP and WebSocket responses."""
from __future__ import annotations


def iso(ts) -> str:
    return ts.isoformat() if hasattr(ts, "isoformat") else str(ts)


def canvas_out(row: dict) -> dict:
    return {
        "id": row["id"],
        "owner_did": row["owner_did"],
        "title": row["title"],
        "status": row["status"],
        "created_at": iso(row["created_at"]),
    }


def element_out(row: dict) -> dict:
    return {
        "id": row["id"],
        "canvas_id": row["canvas_id"],
        "kind": row["kind"],
        "owner_did": row["owner_did"],
        "data": row["data"],
        "created_at": iso(row["created_at"]),
        "updated_at": iso(row["updated_at"]),
    }
