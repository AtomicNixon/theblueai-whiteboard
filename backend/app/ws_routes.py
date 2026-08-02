"""WebSocket route: live canvas op stream.

Client connects with ?token=<bsky-mcp-access-token>&canvas=<id>. We validate
the token, register the connection in the room, send a snapshot, then pump
incoming ops (add/update/delete) to the DB + broadcast to the room.
"""
from __future__ import annotations

import json
import logging
import uuid

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from . import db, ws
from .ai_trigger import extract_tags, schedule_ai_tagged
from .auth import AuthError, validate_token
from .elements import normalize, strip_for_storage
from .serializers import canvas_out, element_out

log = logging.getLogger("whiteboard.wsroute")
router = APIRouter()


@router.websocket("/ws/canvas/{canvas_id}")
async def canvas_ws(ws_conn: WebSocket, canvas_id: str) -> None:
    token = ws_conn.query_params.get("token", "")
    if not token:
        await ws_conn.close(code=4401)
        return

    try:
        user = await validate_token(token)
    except AuthError:
        await ws_conn.close(code=4401)
        return

    canvas = await db.get_canvas(canvas_id)
    if canvas is None:
        await ws_conn.close(code=4404)
        return
    if canvas["status"] != "active":
        await ws_conn.close(code=4410)
        return

    await ws_conn.accept()
    await db.upsert_member(canvas_id, user["did"], user.get("handle", ""))
    await ws.join(canvas_id, ws_conn)

    # Send the current snapshot so the client has full state before live ops.
    snap = await db.get_canvas_snapshot(canvas_id)
    if snap:
        await ws_conn.send_text(
            json.dumps({"op": "snapshot", "canvas": canvas_out(snap["canvas"]),
                        "elements": [element_out(e) for e in snap["elements"]],
                        "me": user["did"]})
        )

    try:
        while True:
            raw = await ws_conn.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            op = msg.get("op")
            if op == "add":
                await _handle_add(ws_conn, canvas_id, user, msg)
            elif op == "update":
                await _handle_update(ws_conn, canvas_id, user, msg)
            elif op == "delete":
                await _handle_delete(ws_conn, canvas_id, user, msg)
            else:
                await ws_conn.send_text(json.dumps({"op": "error", "message": f"unknown op {op}"}))
    except WebSocketDisconnect:
        pass
    finally:
        await ws.leave(canvas_id, ws_conn)


async def _handle_add(ws_conn: WebSocket, canvas_id: str, user: dict, msg: dict) -> None:
    kind = msg.get("kind", "mark")
    if kind not in ("text", "mark"):
        await ws_conn.send_text(json.dumps({"op": "error", "message": "bad kind"}))
        return
    data = msg.get("data", {}) or {}
    eid = uuid.uuid4().hex
    data = normalize(kind, data, eid)
    el = await db.add_element(eid, canvas_id, kind, user["did"], data)
    out = element_out(el)
    await ws.broadcast(canvas_id, {"op": "add", "element": out}, exclude=ws_conn)
    await ws_conn.send_text(json.dumps({"op": "add", "element": out}))

    if kind == "text":
        for tag in extract_tags(str(data.get("text", ""))):
            schedule_ai_tagged(canvas_id, tag)


async def _handle_update(ws_conn: WebSocket, canvas_id: str, user: dict, msg: dict) -> None:
    eid = msg.get("element_id", "")
    data = msg.get("data", {}) or {}
    el = await db.update_element(eid, user["did"], strip_for_storage(data))
    if el is None:
        await ws_conn.send_text(
            json.dumps({"op": "error", "message": "element not found, or it's text you don't own"})
        )
        return
    out = element_out(el)
    await ws.broadcast(canvas_id, {"op": "update", "element": out}, exclude=ws_conn)
    await ws_conn.send_text(json.dumps({"op": "update", "element": out}))


async def _handle_delete(ws_conn: WebSocket, canvas_id: str, user: dict, msg: dict) -> None:
    eid = msg.get("element_id", "")
    ok = await db.delete_element(eid, user["did"])
    if not ok:
        await ws_conn.send_text(json.dumps({"op": "error", "message": "not deletable"}))
        return
    await ws.broadcast(canvas_id, {"op": "delete", "element_id": eid}, exclude=ws_conn)
    await ws_conn.send_text(json.dumps({"op": "delete", "element_id": eid}))

