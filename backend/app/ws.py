"""WebSocket connection manager — one hub per canvas.

Broadcasts element-level ops (add/update/delete) to every connected client
on that canvas. No full-canvas re-sync on every change; clients fetch a
snapshot once on connect, then consume the live op stream.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import WebSocket

log = logging.getLogger("whiteboard.ws")

# canvas_id -> set of connected WebSockets
_rooms: dict[str, set[WebSocket]] = {}
_lock = asyncio.Lock()


async def join(canvas_id: str, ws: WebSocket) -> None:
    async with _lock:
        _rooms.setdefault(canvas_id, set()).add(ws)
    log.info("join canvas=%s clients=%d", canvas_id, len(_rooms.get(canvas_id, ())))


async def leave(canvas_id: str, ws: WebSocket) -> None:
    async with _lock:
        room = _rooms.get(canvas_id)
        if room:
            room.discard(ws)
            if not room:
                _rooms.pop(canvas_id, None)
    log.info("leave canvas=%s", canvas_id)


async def broadcast(canvas_id: str, op: dict[str, Any], exclude: WebSocket | None = None) -> None:
    """Send an op to every client in the room except `exclude` (the sender)."""
    async with _lock:
        room = list(_rooms.get(canvas_id, ()))
    payload = json.dumps(op)
    for ws in room:
        if ws is exclude:
            continue
        try:
            await ws.send_text(payload)
        except Exception:
            # Client likely gone; leave() will clean up on the receive side.
            log.debug("broadcast send failed (client gone?)", exc_info=True)


def room_size(canvas_id: str) -> int:
    return len(_rooms.get(canvas_id, ()))
