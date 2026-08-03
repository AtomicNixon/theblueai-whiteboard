"""Bulk element creation, and the refusal to store images.

Images are converted to a few hundred rectangles client-side (frontend
vectorize.ts) rather than stored. Two consequences the backend must enforce:

  1. An image element must never reach the database. Storing one while its
     binary lives only in the browser leaves a `fileId` pointing at nothing —
     a permanent broken placeholder that no reload can fix.
  2. Those few hundred rectangles must arrive in one request, not one each.
"""
from __future__ import annotations

import copy
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

IMAGE_ELEMENT = {
    "id": "img-abc",
    "type": "image",
    "x": 100, "y": 100, "width": 253, "height": 253,
    "fileId": "2a90e74ff50a9749bcd5eea253019ca362729b47",
    "status": "pending",
    "strokeColor": "transparent", "backgroundColor": "transparent",
    "seed": 12345, "version": 1, "versionNonce": 1, "isDeleted": False,
}


def rect(i: int, group: str = "img-1") -> dict:
    return {
        "type": "rectangle",
        "x": float(i * 3), "y": 10.0, "width": 3.0, "height": 3.0,
        "strokeColor": "transparent", "backgroundColor": "#aabbcc",
        "fillStyle": "solid", "roughness": 0, "opacity": 100,
        "groupIds": [group], "seed": 1000 + i,
    }


# --- images are refused -----------------------------------------------------

def test_image_element_rejected_on_single_add(client, alice_headers, canvas):
    r = client.post(f"/api/canvases/{canvas['id']}/elements", headers=alice_headers,
                    json={"kind": "mark", "data": copy.deepcopy(IMAGE_ELEMENT)})
    assert r.status_code == 400
    assert "never stored" in r.json()["detail"] or "image" in r.json()["detail"].lower()

    snap = client.get(f"/api/canvases/{canvas['id']}/snapshot", headers=alice_headers).json()
    assert snap["elements"] == [], "an image must not reach the database"


def test_image_silently_skipped_in_bulk(client, alice_headers, canvas):
    """A bulk batch with an image mixed in stores the rest and drops the image,
    rather than failing the whole paste."""
    body = {"elements": [
        {"kind": "mark", "data": rect(0)},
        {"kind": "mark", "data": copy.deepcopy(IMAGE_ELEMENT)},
        {"kind": "mark", "data": rect(1)},
    ]}
    r = client.post(f"/api/canvases/{canvas['id']}/elements/bulk",
                    headers=alice_headers, json=body)
    assert r.status_code == 200
    assert len(r.json()) == 2

    snap = client.get(f"/api/canvases/{canvas['id']}/snapshot", headers=alice_headers).json()
    assert len(snap["elements"]) == 2
    assert all(e["data"]["type"] != "image" for e in snap["elements"])


def test_image_rejected_over_websocket(client, canvas):
    with client.websocket_connect(f"/ws/canvas/{canvas['id']}?token=tok-alice") as ws:
        assert ws.receive_json()["op"] == "snapshot"
        ws.send_json({"op": "add", "kind": "mark", "data": copy.deepcopy(IMAGE_ELEMENT)})
        reply = ws.receive_json()
        assert reply["op"] == "error"
        assert "image" in reply["message"].lower()


# --- bulk creation ----------------------------------------------------------

def test_bulk_creates_everything(client, alice_headers, canvas):
    n = 250  # a typical vectorized image
    body = {"elements": [{"kind": "mark", "data": rect(i)} for i in range(n)]}
    r = client.post(f"/api/canvases/{canvas['id']}/elements/bulk",
                    headers=alice_headers, json=body)
    assert r.status_code == 200
    created = r.json()
    assert len(created) == n
    assert len({c["id"] for c in created}) == n, "ids must be unique"

    snap = client.get(f"/api/canvases/{canvas['id']}/snapshot", headers=alice_headers).json()
    assert len(snap["elements"]) == n


def test_bulk_preserves_group_and_colour(client, alice_headers, canvas):
    """groupIds are what make a vectorized image behave as one object."""
    body = {"elements": [{"kind": "mark", "data": rect(i, "picture-7")} for i in range(5)]}
    client.post(f"/api/canvases/{canvas['id']}/elements/bulk", headers=alice_headers, json=body)

    snap = client.get(f"/api/canvases/{canvas['id']}/snapshot", headers=alice_headers).json()
    assert len(snap["elements"]) == 5
    for e in snap["elements"]:
        assert e["data"]["groupIds"] == ["picture-7"]
        assert e["data"]["backgroundColor"] == "#aabbcc"
        assert e["data"]["fillStyle"] == "solid"


def test_bulk_is_capped(client, alice_headers, canvas):
    from app.routes import MAX_BULK
    body = {"elements": [{"kind": "mark", "data": rect(i)} for i in range(MAX_BULK + 1)]}
    r = client.post(f"/api/canvases/{canvas['id']}/elements/bulk",
                    headers=alice_headers, json=body)
    assert r.status_code == 413


def test_bulk_empty_is_harmless(client, alice_headers, canvas):
    r = client.post(f"/api/canvases/{canvas['id']}/elements/bulk",
                    headers=alice_headers, json={"elements": []})
    assert r.status_code == 200
    assert r.json() == []


def test_bulk_broadcasts_one_op(client, alice_headers, canvas):
    """Other clients get a single add_bulk, not 250 separate adds."""
    with client.websocket_connect(f"/ws/canvas/{canvas['id']}?token=tok-bob") as ws:
        assert ws.receive_json()["op"] == "snapshot"

        body = {"elements": [{"kind": "mark", "data": rect(i)} for i in range(30)]}
        client.post(f"/api/canvases/{canvas['id']}/elements/bulk",
                    headers=alice_headers, json=body)

        op = ws.receive_json()
        assert op["op"] == "add_bulk"
        assert len(op["elements"]) == 30
        assert op["elements"][0]["data"]["groupIds"] == ["img-1"]


def test_bulk_attributes_to_the_caller(client, bob_headers, canvas):
    body = {"elements": [{"kind": "mark", "data": rect(i)} for i in range(3)]}
    r = client.post(f"/api/canvases/{canvas['id']}/elements/bulk",
                    headers=bob_headers, json=body)
    assert all(e["owner_did"].endswith("bob00000000000000000000000") for e in r.json())
