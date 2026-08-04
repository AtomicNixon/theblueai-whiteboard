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
from .models import CanvasCreate, ElementIn, ElementsIn, ElementUpdate, FileIn
from .serializers import canvas_out, element_out

log = logging.getLogger("whiteboard.http")
router = APIRouter()
security = HTTPBearer(auto_error=False)

# Bounds a single bulk request so one paste can't push thousands of rows at once.
MAX_BULK = 1000

# Image ceilings, in data-URL characters (base64, so ~4/3 of the real bytes).
# The browser downscales to ~1200px and re-encodes at JPEG q80 before sending,
# which normally lands well under these; they exist so a hand-rolled client
# can't fill the disk.
MAX_FILE_CHARS = 1_500_000          # ~1.1 MB of actual image
MAX_CANVAS_FILE_CHARS = 24_000_000  # ~18 MB per canvas


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


@router.post("/canvases/{canvas_id}/files")
async def upload_file(user: UserDep, canvas_id: str, body: FileIn) -> dict:
    """Store an image for a canvas.

    The browser downscales and re-encodes to JPEG first, so the size ceiling
    here is a backstop rather than the primary bound — but it is a real one:
    without it a single paste could put an arbitrary number of megabytes into
    Postgres on a 2 GB box.
    """
    c = await db.get_canvas(canvas_id)
    if c is None:
        raise HTTPException(404, "canvas not found")
    if c["status"] != "active":
        raise HTTPException(409, "canvas is archived")

    if not body.dataURL.startswith("data:image/"):
        raise HTTPException(400, "not an image data URL")
    if len(body.dataURL) > MAX_FILE_CHARS:
        raise HTTPException(
            413,
            f"Image is {len(body.dataURL) // 1024} KB after compression; "
            f"the limit is {MAX_FILE_CHARS // 1024} KB.",
        )

    used = await db.canvas_files_bytes(canvas_id)
    if used + len(body.dataURL) > MAX_CANVAS_FILE_CHARS:
        raise HTTPException(
            413,
            f"This canvas is already holding {used // 1024} KB of images "
            f"(limit {MAX_CANVAS_FILE_CHARS // 1024} KB). Delete some first.",
        )

    await db.put_file(canvas_id, body.id, body.mimeType, body.dataURL, user["did"])
    await ws.broadcast(canvas_id, {
        "op": "file",
        "file": {"id": body.id, "mimeType": body.mimeType, "dataURL": body.dataURL},
    })
    return {"id": body.id, "bytes": len(body.dataURL)}


@router.post("/canvases/{canvas_id}/files/gc")
async def gc_files(user: UserDep, canvas_id: str) -> dict:
    """Drop images no element references. Called after deletions."""
    if await db.get_canvas(canvas_id) is None:
        raise HTTPException(404, "canvas not found")
    return {"removed": await db.delete_orphan_files(canvas_id)}


@router.get("/canvases/{canvas_id}/snapshot")
async def get_snapshot(user: UserDep, canvas_id: str) -> dict:
    snap = await db.get_canvas_snapshot(canvas_id)
    if snap is None:
        raise HTTPException(404, "canvas not found")
    await db.upsert_member(canvas_id, user["did"], user.get("handle", ""))
    return {
        "canvas": canvas_out(snap["canvas"]),
        "elements": [element_out(e) for e in snap["elements"]],
        # Excalidraw needs these handed to addFiles() before an image element
        # will render; the element only carries a fileId.
        "files": await db.get_files(canvas_id),
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
    if is_ephemeral(body.data) and not await db.get_files(canvas_id):
        # An image element whose file we've never seen. Either the upload failed
        # or this is an out-of-date client; either way it would render as a
        # broken placeholder forever.
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Upload the image before its element (POST .../files). "
            "If you didn't do this by hand, reload the page.",
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

