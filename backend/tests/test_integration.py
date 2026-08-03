"""End-to-end tests against a real Postgres.

These exist to verify the two decisions made on 2026-08-02, at the level where
they can actually fail:

  1. Excalidraw elements are stored VERBATIM. A freehand stroke drawn in a
     browser must come back out of JSONB byte-identical — including
     `simulatePressure` and `pressures`, whose loss in the old translation
     layer rendered strokes zero-width, and `seed`, whose hardcoding to 1
     flattened the hand-drawn roughness.

  2. Marks are free-for-all, text is single-owner. Anyone may move or erase
     any stroke or shape; only the author may edit their own text.

Run:  cd backend && python -m pytest tests/ -v
Needs Docker (a throwaway postgres:16-alpine). Skips cleanly without it.
"""
from __future__ import annotations

import copy

import pytest

# A faithful Excalidraw 0.18 freedraw element, as the browser serializes one
# after a real pen stroke. The float coordinates and irregular pressures matter:
# round numbers would hide a float/JSON precision fault.
BROWSER_FREEDRAW = {
    "id": "vX3kQm7pLnA9",
    "type": "freedraw",
    "x": 412.37109375,
    "y": 208.6484375,
    "width": 143.8203125,
    "height": 97.51171875,
    "angle": 0,
    "strokeColor": "#e03131",
    "backgroundColor": "transparent",
    "fillStyle": "solid",
    "strokeWidth": 2,
    "strokeStyle": "solid",
    "roughness": 1,
    "opacity": 100,
    "groupIds": [],
    "frameId": None,
    "roundness": None,
    "seed": 1904382057,
    "version": 137,
    "versionNonce": 511823045,
    "isDeleted": False,
    "boundElements": None,
    "updated": 1785700000000,
    "link": None,
    "points": [[0, 0], [1.2109375, 3.44921875], [7.359375, 19.05078125],
               [21.140625, 46.72265625], [55.87109375, 78.3125],
               [98.62890625, 93.171875], [143.8203125, 97.51171875]],
    "pressures": [0.21875, 0.4453125, 0.61328125, 0.7734375, 0.828125,
                  0.6015625, 0.203125],
    "simulatePressure": False,
    "lastCommittedPoint": [143.8203125, 97.51171875],
}

BROWSER_TEXT = {
    "id": "tXt99aabbcc",
    "type": "text",
    "x": 100.5, "y": 200.25, "width": 187.5, "height": 25,
    "angle": 0, "strokeColor": "#1e1e1e", "backgroundColor": "transparent",
    "fillStyle": "solid", "strokeWidth": 2, "strokeStyle": "solid",
    "roughness": 1, "opacity": 100, "groupIds": [], "frameId": None,
    "roundness": None, "seed": 88112233, "version": 12, "versionNonce": 99887766,
    "isDeleted": False, "boundElements": None, "updated": 1785700000001,
    "link": None,
    "text": "hello whiteboard", "fontSize": 20, "fontFamily": 1,
    "textAlign": "left", "verticalAlign": "top", "containerId": None,
    "originalText": "hello whiteboard", "lineHeight": 1.25, "autoResize": True,
}

# Set by the client on the way up and re-derived on the way down; never stored.
VIEWER_LOCAL = ("locked", "customData")


def _post(client, headers, canvas_id, kind, data):
    r = client.post(f"/api/canvases/{canvas_id}/elements", headers=headers,
                    json={"kind": kind, "data": data})
    assert r.status_code == 200, r.text
    return r.json()


def _snapshot(client, headers, canvas_id):
    r = client.get(f"/api/canvases/{canvas_id}/snapshot", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


# --------------------------------------------------------------------------
# The headline: verbatim round-trip
# --------------------------------------------------------------------------

def test_freedraw_survives_round_trip_byte_identical(client, alice_headers, canvas):
    """The regression guard for the whole 2026-08-02 change.

    If this fails, the translation layer is back and freehand strokes render
    wrong after the server echoes them.
    """
    sent = copy.deepcopy(BROWSER_FREEDRAW)
    created = _post(client, alice_headers, canvas["id"], "mark", sent)

    snap = _snapshot(client, alice_headers, canvas["id"])
    assert len(snap["elements"]) == 1
    got = snap["elements"][0]["data"]

    # Every field the client sent, unchanged, through JSONB and back.
    for key, value in BROWSER_FREEDRAW.items():
        if key == "isDeleted":
            continue  # server forces False; asserted separately below
        assert got[key] == value, (
            f"{key} changed: sent {value!r}, got {got.get(key)!r}"
        )

    assert got["isDeleted"] is False
    assert created["kind"] == "mark"
    assert created["owner_did"].endswith("alice0000000000000000000")


def test_pressure_fields_specifically(client, alice_headers, canvas):
    """simulatePressure and pressures are what the old code dropped."""
    _post(client, alice_headers, canvas["id"], "mark", copy.deepcopy(BROWSER_FREEDRAW))
    got = _snapshot(client, alice_headers, canvas["id"])["elements"][0]["data"]

    assert got["simulatePressure"] is False, "a client's explicit choice must survive"
    assert got["pressures"] == BROWSER_FREEDRAW["pressures"]
    assert len(got["pressures"]) == len(got["points"]), "pressure/point arity must match"
    assert got["seed"] == 1904382057, "hardcoding seed flattens hand-drawn roughness"
    assert got["versionNonce"] == 511823045


def test_float_precision_preserved(client, alice_headers, canvas):
    """Sub-pixel coordinates must not be rounded by the JSON/JSONB trip."""
    _post(client, alice_headers, canvas["id"], "mark", copy.deepcopy(BROWSER_FREEDRAW))
    got = _snapshot(client, alice_headers, canvas["id"])["elements"][0]["data"]
    assert got["x"] == 412.37109375
    assert got["points"][3] == [21.140625, 46.72265625]


def test_viewer_local_fields_never_persist(client, alice_headers, canvas):
    """`locked` is computed per viewer — storing it imposes it on everyone."""
    poisoned = dict(copy.deepcopy(BROWSER_FREEDRAW), locked=True,
                    customData={"wbid": "stale-id", "owner": "someone-else"})
    _post(client, alice_headers, canvas["id"], "mark", poisoned)
    got = _snapshot(client, alice_headers, canvas["id"])["elements"][0]["data"]
    for field in VIEWER_LOCAL:
        assert field not in got, f"{field} must be stripped before storage"


# --------------------------------------------------------------------------
# AI-authored partial elements
# --------------------------------------------------------------------------

def test_ai_partial_text_is_completed(client, bob_headers, canvas):
    """Exactly the payload bsky-mcp's wb_add_text posts."""
    _post(client, bob_headers, canvas["id"], "text",
          {"text": "hello from @bob", "x": 100, "y": 100, "width": 200, "height": 40})
    got = _snapshot(client, bob_headers, canvas["id"])["elements"][0]["data"]

    # Enough to render in Excalidraw without the client patching it up.
    for field in ("id", "type", "fontSize", "fontFamily", "textAlign",
                  "verticalAlign", "originalText", "lineHeight", "seed",
                  "versionNonce", "groupIds", "roundness"):
        assert field in got, f"AI element missing required field {field}"
    assert got["type"] == "text"
    assert got["text"] == "hello from @bob"
    assert got["originalText"] == "hello from @bob"


def test_ai_partial_mark_is_renderable(client, bob_headers, canvas):
    """wb_add_mark's payload — a shape with no Excalidraw internals."""
    _post(client, bob_headers, canvas["id"], "mark",
          {"type": "rectangle", "x": 10, "y": 20, "width": 100, "height": 50,
           "points": [[0, 0], [100, 50]]})
    got = _snapshot(client, bob_headers, canvas["id"])["elements"][0]["data"]
    assert got["type"] == "rectangle"
    assert got["seed"] != 1, "each element needs its own roughness seed"
    for field in ("strokeColor", "fillStyle", "opacity", "versionNonce"):
        assert field in got


# --------------------------------------------------------------------------
# Ownership: text single-owner, marks free-for-all
# --------------------------------------------------------------------------

def test_bob_can_move_alices_mark(client, alice_headers, bob_headers, canvas):
    """The 2026-08-02 rule change. Chaos is a feature."""
    created = _post(client, alice_headers, canvas["id"], "mark",
                    copy.deepcopy(BROWSER_FREEDRAW))
    moved = dict(copy.deepcopy(BROWSER_FREEDRAW), x=999.5, y=888.25)

    r = client.patch(f"/api/elements/{created['id']}", headers=bob_headers,
                     json={"data": moved})
    assert r.status_code == 200, f"marks must be free-for-all: {r.text}"

    got = _snapshot(client, alice_headers, canvas["id"])["elements"][0]["data"]
    assert got["x"] == 999.5, "the move must persist, not just move locally"
    assert got["y"] == 888.25


def test_bob_cannot_edit_alices_text(client, alice_headers, bob_headers, canvas):
    created = _post(client, alice_headers, canvas["id"], "text",
                    copy.deepcopy(BROWSER_TEXT))
    hijack = dict(copy.deepcopy(BROWSER_TEXT), text="bob was here")

    r = client.patch(f"/api/elements/{created['id']}", headers=bob_headers,
                     json={"data": hijack})
    assert r.status_code == 403, "text is single-owner"

    got = _snapshot(client, alice_headers, canvas["id"])["elements"][0]["data"]
    assert got["text"] == "hello whiteboard", "content must be unchanged"


def test_alice_can_edit_her_own_text(client, alice_headers, canvas):
    created = _post(client, alice_headers, canvas["id"], "text",
                    copy.deepcopy(BROWSER_TEXT))
    edited = dict(copy.deepcopy(BROWSER_TEXT), text="edited by the owner")
    r = client.patch(f"/api/elements/{created['id']}", headers=alice_headers,
                     json={"data": edited})
    assert r.status_code == 200, r.text
    got = _snapshot(client, alice_headers, canvas["id"])["elements"][0]["data"]
    assert got["text"] == "edited by the owner"


def test_bob_can_erase_alices_mark(client, alice_headers, bob_headers, canvas):
    created = _post(client, alice_headers, canvas["id"], "mark",
                    copy.deepcopy(BROWSER_FREEDRAW))
    r = client.delete(f"/api/elements/{created['id']}", headers=bob_headers)
    assert r.status_code == 200, r.text
    assert _snapshot(client, alice_headers, canvas["id"])["elements"] == []


def test_bob_cannot_delete_alices_text(client, alice_headers, bob_headers, canvas):
    created = _post(client, alice_headers, canvas["id"], "text",
                    copy.deepcopy(BROWSER_TEXT))
    r = client.delete(f"/api/elements/{created['id']}", headers=bob_headers)
    assert r.status_code == 403
    assert len(_snapshot(client, alice_headers, canvas["id"])["elements"]) == 1


# --------------------------------------------------------------------------
# Live sync over WebSocket
# --------------------------------------------------------------------------

def test_ws_snapshot_on_connect(client, alice_headers, canvas):
    _post(client, alice_headers, canvas["id"], "mark", copy.deepcopy(BROWSER_FREEDRAW))
    with client.websocket_connect(f"/ws/canvas/{canvas['id']}?token=tok-alice") as ws:
        msg = ws.receive_json()
        assert msg["op"] == "snapshot"
        assert msg["me"].endswith("alice0000000000000000000")
        assert len(msg["elements"]) == 1
        assert msg["elements"][0]["data"]["pressures"] == BROWSER_FREEDRAW["pressures"]


def test_alices_stroke_reaches_bobs_browser(client, alice_headers, canvas):
    """The 'second browser sees it' check, without a second browser."""
    with client.websocket_connect(f"/ws/canvas/{canvas['id']}?token=tok-bob") as bob_ws:
        assert bob_ws.receive_json()["op"] == "snapshot"

        _post(client, alice_headers, canvas["id"], "mark",
              copy.deepcopy(BROWSER_FREEDRAW))

        op = bob_ws.receive_json()
        assert op["op"] == "add"
        got = op["element"]["data"]
        # Arriving intact matters as much as arriving.
        assert got["pressures"] == BROWSER_FREEDRAW["pressures"]
        assert got["simulatePressure"] is False
        assert got["seed"] == BROWSER_FREEDRAW["seed"]
        assert got["points"] == BROWSER_FREEDRAW["points"]


def test_ws_add_then_broadcast(client, canvas):
    """An element created over the WS transport reaches other clients."""
    with client.websocket_connect(f"/ws/canvas/{canvas['id']}?token=tok-alice") as a, \
         client.websocket_connect(f"/ws/canvas/{canvas['id']}?token=tok-bob") as b:
        assert a.receive_json()["op"] == "snapshot"
        assert b.receive_json()["op"] == "snapshot"

        a.send_json({"op": "add", "kind": "mark",
                     "data": copy.deepcopy(BROWSER_FREEDRAW)})

        echo = a.receive_json()          # sender's own echo, carries the backend id
        assert echo["op"] == "add"
        assert echo["element"]["id"]

        relayed = b.receive_json()
        assert relayed["op"] == "add"
        assert relayed["element"]["data"]["pressures"] == BROWSER_FREEDRAW["pressures"]


def test_ws_rejects_bad_token(client, canvas):
    from starlette.websockets import WebSocketDisconnect
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f"/ws/canvas/{canvas['id']}?token=nope") as ws:
            ws.receive_json()


# --------------------------------------------------------------------------
# Persistence across reconnect — "survives a reload"
# --------------------------------------------------------------------------

def test_stroke_survives_reload(client, alice_headers, canvas):
    """Draw, disconnect, reconnect. What comes back must be what was drawn."""
    _post(client, alice_headers, canvas["id"], "mark", copy.deepcopy(BROWSER_FREEDRAW))

    with client.websocket_connect(f"/ws/canvas/{canvas['id']}?token=tok-alice") as ws:
        first = ws.receive_json()["elements"][0]["data"]

    with client.websocket_connect(f"/ws/canvas/{canvas['id']}?token=tok-alice") as ws:
        second = ws.receive_json()["elements"][0]["data"]

    assert first == second, "reconnect must be deterministic"
    assert second["points"] == BROWSER_FREEDRAW["points"]
    assert second["pressures"] == BROWSER_FREEDRAW["pressures"]
