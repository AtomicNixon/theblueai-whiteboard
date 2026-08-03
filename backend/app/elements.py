"""Excalidraw element normalization.

The browser stores Excalidraw elements *verbatim* in `canvas_elements.data` —
we do not decompose them into our own schema and rebuild them on the way out.
That translation layer was where every rendering bug lived (missing
`simulatePressure`, hardcoded `seed`/`versionNonce`), and it re-coupled us to
Excalidraw's internals on every release.

But non-browser clients (AI agents via bsky-mcp's `wb_add_text` / `wb_add_mark`)
post a *partial* element — just text/position — because they have no business
knowing Excalidraw's internal shape. So the completion happens here, once, on
the server: a full element passes through untouched, a partial one gets the
required fields filled in.

Rule of thumb: if the client already sent a field, we never overwrite it.
"""
from __future__ import annotations

import random
import time
from typing import Any

# Fields we refuse to persist regardless of what the client sends.
#   locked     — computed per-viewer (other users' text is locked in their UI),
#                so one viewer's lock state must never become everyone's.
#   isDeleted  — deletion is a row delete here; a stored `isDeleted: true`
#                element would be an invisible ghost that never goes away.
#   customData — carries {wbid, owner}, both of which are authoritative columns
#                on the row. Re-derived on read so there is one source of truth.
_STRIPPED = ("locked", "isDeleted", "customData")

# Excalidraw treats these as required on every element.
_BASE_DEFAULTS: dict[str, Any] = {
    "x": 100.0,
    "y": 100.0,
    "width": 100.0,
    "height": 100.0,
    "angle": 0,
    "strokeColor": "#1e1e1e",
    "backgroundColor": "transparent",
    "fillStyle": "solid",
    "strokeWidth": 2,
    "strokeStyle": "solid",
    "roughness": 1,
    "opacity": 100,
    "groupIds": [],
    "frameId": None,
    "roundness": None,
    "boundElements": None,
    "link": None,
}

_TEXT_DEFAULTS: dict[str, Any] = {
    "type": "text",
    "fontSize": 20,
    "fontFamily": 1,        # 1 = Excalifont (hand-drawn)
    "textAlign": "left",
    "verticalAlign": "top",
    "containerId": None,
    "lineHeight": 1.25,
    "autoResize": True,
}

_FREEDRAW_DEFAULTS: dict[str, Any] = {
    "pressures": [],
    # Without this, perfect-freehand gets an empty pressure array and renders a
    # zero-width stroke. This is the field whose absence broke round-tripped
    # freehand marks in the old translation layer.
    "simulatePressure": True,
    "lastCommittedPoint": None,
}

_LINEAR_DEFAULTS: dict[str, Any] = {
    "lastCommittedPoint": None,
    "startBinding": None,
    "endBinding": None,
    "startArrowhead": None,
    "endArrowhead": None,
}

_LINEAR_TYPES = ("line", "arrow")
_SHAPE_TYPES = ("rectangle", "ellipse", "diamond")


def _nonce() -> int:
    return random.randint(1, 2**31 - 1)


def is_ephemeral(data: dict[str, Any]) -> bool:
    """Images are relayed live and never stored. See ephemeral.py.

    Persisting the element while dropping its file would leave a `fileId`
    pointing at bytes that no longer exist — a permanent broken placeholder
    that never resolves. If the picture is ephemeral, so is its element.
    """
    return (data or {}).get("type") == "image"


def normalize(kind: str, data: dict[str, Any], element_id: str) -> dict[str, Any]:
    """Return a complete, renderable Excalidraw element.

    `element_id` is our backend row id, used to derive a stable Excalidraw id
    when the client didn't supply one (i.e. an AI-authored element).
    """
    el = {k: v for k, v in (data or {}).items() if k not in _STRIPPED}

    # --- identity -----------------------------------------------------------
    # Browser-drawn elements arrive with Excalidraw's own id; keep it, since the
    # client correlates its live scene by that id. AI-authored ones get a stable
    # id derived from the row so repeated reads don't produce duplicates.
    if not el.get("id"):
        el["id"] = f"wb{element_id[:20]}"

    # --- type ---------------------------------------------------------------
    if not el.get("type"):
        el["type"] = "text" if kind == "text" else "freedraw"

    etype = el["type"]

    for key, val in _BASE_DEFAULTS.items():
        el.setdefault(key, val)

    # Excalidraw mutates these as the user edits; seed drives the hand-drawn
    # roughness, so a constant would make every shape look identical.
    el.setdefault("seed", _nonce())
    el.setdefault("versionNonce", _nonce())
    el.setdefault("version", 1)
    el.setdefault("updated", int(time.time() * 1000))
    el["isDeleted"] = False

    # --- per-type completion ------------------------------------------------
    if etype == "text":
        for key, val in _TEXT_DEFAULTS.items():
            el.setdefault(key, val)
        el.setdefault("text", "")
        # Excalidraw keeps the pre-wrap source in originalText; if a caller only
        # gave us `text`, they're the same string.
        el.setdefault("originalText", el["text"])

    elif etype == "freedraw":
        for key, val in _FREEDRAW_DEFAULTS.items():
            el.setdefault(key, val)
        if not el.get("points"):
            el["points"] = [[0, 0], [el["width"], el["height"]]]

    elif etype in _LINEAR_TYPES:
        for key, val in _LINEAR_DEFAULTS.items():
            el.setdefault(key, val)
        if not el.get("points"):
            el["points"] = [[0, 0], [el["width"], el["height"]]]

    elif etype in _SHAPE_TYPES:
        # Shapes carry no extra required fields beyond the base set.
        pass

    return el


def strip_for_storage(data: dict[str, Any]) -> dict[str, Any]:
    """Drop viewer-specific / authoritative-elsewhere fields on update.

    Updates come from a browser that already holds a complete element, so this
    is the write-side counterpart to `normalize` without the completion pass.
    """
    el = {k: v for k, v in (data or {}).items() if k not in _STRIPPED}
    el["isDeleted"] = False
    return el
