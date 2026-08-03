"""HTTP routes: canvas CRUD, element add/update/delete, snapshot."""
from __future__ import annotations

import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from . import db, ws
from .ai_trigger import extract_tags, schedule_ai_tagged
from .auth import S2S_ACTOR_HEADER, S2S_SECRET_HEADER, AuthError, authenticate
from .elements import is_ephemeral, normalize, strip_for_storage
from .models import CanvasCreate, ElementIn, ElementsIn, ElementUpdate
from .serializers import canvas_out, element_out

log = logging.getLogger("whiteboard.http")
router = APIRouter()
security = HTTPBearer(auto_error=False)

# A vectorized image is a few hundred rectangles. This bounds a single request
# so one paste can't be used to push thousands of rows in at once.
MAX_BULK = 1000


async def current_user(
    request: Request,
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(security)],
) -> dict[str, str]:
    """Identity from either a browser's bearer token or a bsky-mcp S2S call.

    AI agents arrive via bsky-mcp's wb_* tools, which hold no bearer token —
    they present the shared secret plus the actor's DID instead. See
    auth.authenticate.
    """
    s2s = request.headers.get(S2S_SECRET_HEADER)
    bearer = creds.credentials if creds and creds.scheme.lower() == "bearer" else None
    if s2s is None and bearer is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing bearer token")
    try:
        return await authenticate(bearer, s2s, request.headers.get(S2S_ACTOR_HEADER))
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
    if is_ephemeral(body.data):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Images are shared live over the WebSocket and never stored. "
            "Send them as an 'image' op, not as an element.",
        )

    eid = uuid.uuid4().hex
    # Browser clients post a complete Excalidraw element and pass through
    # untouched; AI clients post {text, x, y} and get completed here.
    data = normalize(body.kind, body.data, eid)
    el = await db.add_element(eid, canvas_id, body.kind, user["did"], data)

    # AI trigger seam: if a text element tags an AI, fire the (stub) wake in
    # the background so this response isn't blocked by the mention post.
    if body.kind == "text":
        text = str(body.data.get("text", ""))
        for tag in extract_tags(text):
            schedule_ai_tagged(canvas_id, tag)

    out = element_out(el)
    await ws.broadcast(canvas_id, {"op": "add", "element": out})
    return out


@router.post("/canvases/{canvas_id}/elements/bulk")
async def add_elements_bulk(user: UserDep, canvas_id: str, body: ElementsIn) -> list[dict]:
    """Create many elements in one request.

    A vectorized image is a few hundred rectangles arriving at once; one POST
    each would be hundreds of round trips for a single paste. Broadcast is a
    single 'add_bulk' op for the same reason.
    """
    c = await db.get_canvas(canvas_id)
    if c is None:
        raise HTTPException(404, "canvas not found")
    if c["status"] != "active":
        raise HTTPException(409, "canvas is archived")
    if len(body.elements) > MAX_BULK:
        raise HTTPException(413, f"too many elements in one request (max {MAX_BULK})")

    out: list[dict] = []
    for item in body.elements:
        if is_ephemeral(item.data):
            continue  # images are vectorized client-side; never stored
        eid = uuid.uuid4().hex
        data = normalize(item.kind, item.data, eid)
        el = await db.add_element(eid, canvas_id, item.kind, user["did"], data)
        out.append(element_out(el))
        if item.kind == "text":
            for tag in extract_tags(str(item.data.get("text", ""))):
                schedule_ai_tagged(canvas_id, tag)

    if out:
        await ws.broadcast(canvas_id, {"op": "add_bulk", "elements": out})
    return out


@router.patch("/elements/{element_id}")
async def update_element(user: UserDep, element_id: str, body: ElementUpdate) -> dict:
    # update_element enforces the ownership split at the DB layer: text is
    # owner-only, marks are free-for-all.
    el = await db.update_element(element_id, user["did"], strip_for_storage(body.data))
    if el is None:
        raise HTTPException(403, "element not found, or it's text you don't own")
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

