"""Bulk element creation, and image storage.

Images are downscaled and JPEG re-encoded in the browser, then stored in
canvas_files keyed by Excalidraw's own fileId. The element only carries that
fileId, so the invariant that matters is: an image element must never outlive
(or precede) its bytes, or it renders as a permanent broken placeholder.

This replaced a vectorizer that turned images into a few hundred shapes. It was
measured carefully and dropped — at any element budget we'd tolerate you
couldn't tell what the picture was. See app/images.ts for the history.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# A 1x1 JPEG; contents don't matter, only that it's a well-formed image data URL.
TINY_JPEG = (
    "data:image/jpeg;base64,/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsL"
    "DBkSEw8UHRofHh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/wAALCAABAAEBAREA/8QAFAAB"
    "AAAAAAAAAAAAAAAAAAAACf/EABQQAQAAAAAAAAAAAAAAAAAAAAD/2gAIAQEAAD8AKp//2Q=="
)


def rect(i: int, group: str = "img-1") -> dict:
    return {
        "type": "rectangle", "x": float(i * 3), "y": 10.0, "width": 3.0, "height": 3.0,
        "strokeColor": "transparent", "backgroundColor": "#aabbcc", "fillStyle": "solid",
        "roughness": 0, "opacity": 100, "groupIds": [group], "seed": 1000 + i,
    }


def image_element(file_id: str = "file-abc") -> dict:
    return {
        "id": "img-el-1", "type": "image", "x": 10, "y": 10, "width": 200, "height": 150,
        "fileId": file_id, "status": "saved",
        "strokeColor": "transparent", "backgroundColor": "transparent",
        "seed": 4242, "version": 1, "versionNonce": 1, "isDeleted": False,
    }


def upload(client, headers, canvas_id, file_id="file-abc", data_url=TINY_JPEG):
    return client.post(f"/api/canvases/{canvas_id}/files", headers=headers,
                       json={"id": file_id, "mimeType": "image/jpeg", "dataURL": data_url})


# --- storing an image -------------------------------------------------------

def test_upload_then_element(client, alice_headers, canvas):
    assert upload(client, alice_headers, canvas["id"]).status_code == 200

    r = client.post(f"/api/canvases/{canvas['id']}/elements", headers=alice_headers,
                    json={"kind": "mark", "data": image_element()})
    assert r.status_code == 200, r.text

    snap = client.get(f"/api/canvases/{canvas['id']}/snapshot", headers=alice_headers).json()
    assert len(snap["elements"]) == 1
    assert len(snap["files"]) == 1
    assert snap["files"][0]["id"] == "file-abc"
    assert snap["files"][0]["dataURL"].startswith("data:image/jpeg")


def test_element_without_its_file_is_refused(client, alice_headers, canvas):
    """The invariant: no element pointing at bytes we don't have."""
    r = client.post(f"/api/canvases/{canvas['id']}/elements", headers=alice_headers,
                    json={"kind": "mark", "data": image_element()})
    assert r.status_code == 400
    assert "upload" in r.json()["detail"].lower()


def test_snapshot_carries_files_for_everyone(client, alice_headers, bob_headers, canvas):
    """Excalidraw needs addFiles() before an image element will render, so the
    file has to reach every viewer, not just whoever pasted it."""
    upload(client, alice_headers, canvas["id"])
    client.post(f"/api/canvases/{canvas['id']}/elements", headers=alice_headers,
                json={"kind": "mark", "data": image_element()})

    snap = client.get(f"/api/canvases/{canvas['id']}/snapshot", headers=bob_headers).json()
    assert len(snap["files"]) == 1


def test_upload_is_idempotent(client, alice_headers, canvas):
    """The same paste can arrive twice; it must not duplicate."""
    assert upload(client, alice_headers, canvas["id"]).status_code == 200
    assert upload(client, alice_headers, canvas["id"]).status_code == 200
    snap = client.get(f"/api/canvases/{canvas['id']}/snapshot", headers=alice_headers).json()
    assert len(snap["files"]) == 1


def test_non_image_rejected(client, alice_headers, canvas):
    r = client.post(f"/api/canvases/{canvas['id']}/files", headers=alice_headers,
                    json={"id": "x", "mimeType": "text/html",
                          "dataURL": "data:text/html;base64,PHNjcmlwdD4="})
    assert r.status_code == 400


# --- the size ceiling -------------------------------------------------------

def test_oversized_image_rejected(client, alice_headers, canvas):
    """The browser downscales first; this is the backstop that stops a
    hand-rolled client putting arbitrary megabytes into Postgres."""
    from app.routes import MAX_FILE_CHARS
    huge = "data:image/jpeg;base64," + ("A" * (MAX_FILE_CHARS + 10))
    r = client.post(f"/api/canvases/{canvas['id']}/files", headers=alice_headers,
                    json={"id": "huge", "mimeType": "image/jpeg", "dataURL": huge})
    assert r.status_code == 413
    assert "limit" in r.json()["detail"].lower()


def test_per_canvas_budget_enforced(client, alice_headers, canvas):
    from app.routes import MAX_CANVAS_FILE_CHARS, MAX_FILE_CHARS
    chunk = "data:image/jpeg;base64," + ("A" * (MAX_FILE_CHARS - 100))
    n = MAX_CANVAS_FILE_CHARS // MAX_FILE_CHARS + 1
    codes = [
        client.post(f"/api/canvases/{canvas['id']}/files", headers=alice_headers,
                    json={"id": f"f{i}", "mimeType": "image/jpeg", "dataURL": chunk}).status_code
        for i in range(n + 1)
    ]
    assert 413 in codes, "a canvas must not accept unbounded image bytes"


# --- garbage collection -----------------------------------------------------

def test_orphan_files_collected(client, alice_headers, canvas):
    """Deleting an image element leaves its bytes behind; gc reclaims them."""
    upload(client, alice_headers, canvas["id"])
    created = client.post(f"/api/canvases/{canvas['id']}/elements", headers=alice_headers,
                          json={"kind": "mark", "data": image_element()}).json()

    r = client.post(f"/api/canvases/{canvas['id']}/files/gc", headers=alice_headers)
    assert r.json()["removed"] == 0, "a referenced file must not be collected"

    client.delete(f"/api/elements/{created['id']}", headers=alice_headers)
    r = client.post(f"/api/canvases/{canvas['id']}/files/gc", headers=alice_headers)
    assert r.json()["removed"] == 1

    snap = client.get(f"/api/canvases/{canvas['id']}/snapshot", headers=alice_headers).json()
    assert snap["files"] == []


# --- bulk creation ----------------------------------------------------------

def test_bulk_creates_everything(client, alice_headers, canvas):
    n = 250
    body = {"elements": [{"kind": "mark", "data": rect(i)} for i in range(n)]}
    r = client.post(f"/api/canvases/{canvas['id']}/elements/bulk",
                    headers=alice_headers, json=body)
    assert r.status_code == 200
    created = r.json()
    assert len(created) == n
    assert len({c["id"] for c in created}) == n


def test_bulk_preserves_group_and_colour(client, alice_headers, canvas):
    body = {"elements": [{"kind": "mark", "data": rect(i, "picture-7")} for i in range(5)]}
    client.post(f"/api/canvases/{canvas['id']}/elements/bulk", headers=alice_headers, json=body)
    snap = client.get(f"/api/canvases/{canvas['id']}/snapshot", headers=alice_headers).json()
    for e in snap["elements"]:
        assert e["data"]["groupIds"] == ["picture-7"]
        assert e["data"]["backgroundColor"] == "#aabbcc"


def test_bulk_is_capped(client, alice_headers, canvas):
    from app.routes import MAX_BULK
    body = {"elements": [{"kind": "mark", "data": rect(i)} for i in range(MAX_BULK + 1)]}
    r = client.post(f"/api/canvases/{canvas['id']}/elements/bulk",
                    headers=alice_headers, json=body)
    assert r.status_code == 413


def test_bulk_broadcasts_one_op(client, alice_headers, canvas):
    with client.websocket_connect(f"/ws/canvas/{canvas['id']}?token=tok-bob") as ws:
        assert ws.receive_json()["op"] == "snapshot"
        body = {"elements": [{"kind": "mark", "data": rect(i)} for i in range(30)]}
        client.post(f"/api/canvases/{canvas['id']}/elements/bulk",
                    headers=alice_headers, json=body)
        op = ws.receive_json()
        assert op["op"] == "add_bulk"
        assert len(op["elements"]) == 30


def test_uploaded_file_is_broadcast(client, alice_headers, canvas):
    """Someone already on the canvas must see a pasted image without reloading."""
    with client.websocket_connect(f"/ws/canvas/{canvas['id']}?token=tok-bob") as ws:
        assert ws.receive_json()["op"] == "snapshot"
        upload(client, alice_headers, canvas["id"])
        op = ws.receive_json()
        assert op["op"] == "file"
        assert op["file"]["id"] == "file-abc"
