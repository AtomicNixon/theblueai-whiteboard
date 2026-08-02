"""HTTP routes: canvas CRUD, element add/update/delete, snapshot."""
from __future__ import annotations

import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from . import db, ws
from .ai_trigger import extract_tags, schedule_ai_tagged
from .auth import AuthError, validate_token
from .models import CanvasCreate, ElementIn, ElementUpdate
from .serializers import canvas_out, element_out

log = logging.getLogger("whiteboard.http")
router = APIRouter()
security = HTTPBearer(auto_error=False)


async def current_user(
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(security)],
) -> dict[str, str]:
    if creds is None or creds.scheme.lower() != "bearer":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing bearer token")
    try:
        return await validate_token(creds.credentials)
    except AuthError as e:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(e)) from e


UserDep = Annotated[dict[str, str], Depends(current_user)]


# --- Canvas ---


@router.post("/canvases")
async def create_canvas(user: UserDep, body: CanvasCreate) -> dict:
    cid = uuid.uuid4().hex
    canvas = await db.create_canvas(cid, user["did"], body.title)
    return canvas_out(canvas)


@router.get("/canvases")
async def list_my_canvases(user: UserDep) -> list[dict]:
    rows = await db.list_canvases_for(user["did"])
    return [canvas_out(r) for r in rows]


@router.get("/canvases/{canvas_id}")
async def get_canvas(user: UserDep, canvas_id: str) -> dict:
    c = await db.get_canvas(canvas_id)
    if c is None:
        raise HTTPException(404, "canvas not found")
    return canvas_out(c)


@router.post("/canvases/{canvas_id}/archive")
async def archive_canvas(user: UserDep, canvas_id: str) -> dict:
    c = await db.get_canvas(canvas_id)
    if c is None:
        raise HTTPException(404, "canvas not found")
    if c["owner_did"] != user["did"]:
        raise HTTPException(403, "only the owner can archive")
    await db.set_canvas_status(canvas_id, "archived")
    return {"id": canvas_id, "status": "archived"}


@router.post("/canvases/{canvas_id}/restore")
async def restore_canvas(user: UserDep, canvas_id: str) -> dict:
    c = await db.get_canvas(canvas_id)
    if c is None:
        raise HTTPException(404, "canvas not found")
    if c["owner_did"] != user["did"]:
        raise HTTPException(403, "only the owner can restore")
    await db.set_canvas_status(canvas_id, "active")
    return {"id": canvas_id, "status": "active"}


@router.get("/canvases/{canvas_id}/snapshot")
async def get_snapshot(user: UserDep, canvas_id: str) -> dict:
    snap = await db.get_canvas_snapshot(canvas_id)
    if snap is None:
        raise HTTPException(404, "canvas not found")
    await db.upsert_member(canvas_id, user["did"], user.get("handle", ""))
    return {
        "canvas": canvas_out(snap["canvas"]),
        "elements": [element_out(e) for e in snap["elements"]],
        "me": user["did"],
    }


# --- Elements ---


@router.post("/canvases/{canvas_id}/elements")
async def add_element(user: UserDep, canvas_id: str, body: ElementIn) -> dict:
    c = await db.get_canvas(canvas_id)
    if c is None:
        raise HTTPException(404, "canvas not found")
    if c["status"] != "active":
        raise HTTPException(409, "canvas is archived")
    eid = uuid.uuid4().hex
    el = await db.add_element(eid, canvas_id, body.kind, user["did"], body.data)

    # AI trigger seam: if a text element tags an AI, fire the (stub) wake in
    # the background so this response isn't blocked by the mention post.
    if body.kind == "text":
        text = str(body.data.get("text", ""))
        for tag in extract_tags(text):
            schedule_ai_tagged(canvas_id, tag)

    out = element_out(el)
    await ws.broadcast(canvas_id, {"op": "add", "element": out})
    return out


@router.patch("/elements/{element_id}")
async def update_element(user: UserDep, element_id: str, body: ElementUpdate) -> dict:
    # update_element enforces owner-only + kind=text at the DB layer.
    el = await db.update_element(element_id, user["did"], body.data)
    if el is None:
        raise HTTPException(403, "element not found or not yours to edit")
    out = element_out(el)
    await ws.broadcast(el["canvas_id"], {"op": "update", "element": out})
    return out


@router.delete("/elements/{element_id}")
async def delete_element(user: UserDep, element_id: str) -> dict:
    el = await db.get_element(element_id)
    if el is None:
        raise HTTPException(404, "element not found")
    ok = await db.delete_element(element_id, user["did"])
    if not ok:
        raise HTTPException(403, "element not found or not yours to delete")
    await ws.broadcast(el["canvas_id"], {"op": "delete", "element_id": element_id})
    return {"deleted": element_id}

