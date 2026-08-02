"""Tests for Excalidraw element normalization.

The contract these lock down:
  - A browser-drawn element passes through byte-identical (minus stripped fields).
    This is the whole point of storing elements verbatim; if it regresses we're
    back to the translation layer that dropped `simulatePressure`.
  - A partial element from an AI agent (bsky-mcp's wb_add_text / wb_add_mark)
    comes out complete and renderable.

Run standalone:  python tests/test_elements.py
Or under pytest: pytest tests/
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.elements import normalize, strip_for_storage  # noqa: E402

REQUIRED_BASE = {
    "id", "type", "x", "y", "width", "height", "angle", "strokeColor",
    "backgroundColor", "fillStyle", "strokeWidth", "strokeStyle", "roughness",
    "opacity", "groupIds", "frameId", "roundness", "seed", "version",
    "versionNonce", "isDeleted", "boundElements", "updated", "link",
}
REQUIRED_TEXT = REQUIRED_BASE | {
    "text", "fontSize", "fontFamily", "textAlign", "verticalAlign",
    "containerId", "originalText", "lineHeight",
}
REQUIRED_FREEDRAW = REQUIRED_BASE | {
    "points", "pressures", "simulatePressure", "lastCommittedPoint",
}

# Exactly the payload bsky-mcp's wb_add_text posts.
AI_TEXT = {"text": "hello @bob", "x": 100, "y": 100, "width": 200, "height": 40}

# Exactly the payload bsky-mcp's wb_add_mark posts.
AI_MARK = {"type": "rectangle", "x": 10, "y": 20, "width": 100, "height": 50,
           "points": [[0, 0], [100, 50]]}

# A complete element as Excalidraw 0.18 serializes it, plus the two viewer-local
# fields the client is expected to strip.
BROWSER_FREEDRAW = {
    "id": "exc-xyz", "type": "freedraw", "x": 5.5, "y": 6.5, "width": 80,
    "height": 90, "angle": 0, "strokeColor": "#e03131",
    "backgroundColor": "transparent", "fillStyle": "solid", "strokeWidth": 4,
    "strokeStyle": "solid", "roughness": 0, "opacity": 100, "groupIds": [],
    "frameId": None, "roundness": None, "seed": 998877, "version": 42,
    "versionNonce": 123456, "isDeleted": False, "boundElements": None,
    "updated": 1, "link": None, "points": [[0, 0], [3, 4], [9, 12]],
    "pressures": [0.1, 0.5, 0.9], "simulatePressure": False,
    "lastCommittedPoint": [9, 12],
    "locked": True, "customData": {"wbid": "stale"},
}


def test_ai_text_is_completed():
    el = normalize("text", AI_TEXT, "abc123def456")
    assert not REQUIRED_TEXT - set(el), f"missing {sorted(REQUIRED_TEXT - set(el))}"
    assert el["type"] == "text"
    assert el["originalText"] == "hello @bob"
    assert el["id"] == "wbabc123def456", "id must be derived from the row id"


def test_ai_mark_is_completed_and_keeps_its_type():
    el = normalize("mark", AI_MARK, "row2")
    assert not REQUIRED_BASE - set(el), f"missing {sorted(REQUIRED_BASE - set(el))}"
    assert el["type"] == "rectangle"


def test_typeless_mark_becomes_valid_freedraw():
    """simulatePressure is the field whose absence rendered zero-width strokes."""
    el = normalize("mark", {"x": 0, "y": 0}, "row3")
    assert not REQUIRED_FREEDRAW - set(el), f"missing {sorted(REQUIRED_FREEDRAW - set(el))}"
    assert el["simulatePressure"] is True
    assert el["points"], "freedraw needs at least a degenerate point list"


def test_browser_element_passes_through_untouched():
    el = normalize("mark", BROWSER_FREEDRAW, "row4")
    for field in ("seed", "version", "versionNonce", "points", "pressures",
                  "lastCommittedPoint", "strokeColor", "strokeWidth", "roughness"):
        assert el[field] == BROWSER_FREEDRAW[field], f"{field} was altered"
    # A client that deliberately disabled pressure simulation keeps that choice.
    assert el["simulatePressure"] is False


def test_viewer_local_fields_are_stripped():
    el = normalize("mark", BROWSER_FREEDRAW, "row4")
    assert "locked" not in el, "one viewer's lock state must not become everyone's"
    assert "customData" not in el, "wbid/owner are authoritative columns"
    assert el["isDeleted"] is False, "soft-deleted ghosts must never persist"


def test_seeds_are_not_shared():
    a = normalize("mark", {"type": "ellipse"}, "r5")
    b = normalize("mark", {"type": "ellipse"}, "r6")
    assert a["seed"] != b["seed"], "shared seed makes every shape look identical"


def test_strip_for_storage():
    out = strip_for_storage(
        {"text": "edited", "locked": True, "isDeleted": True,
         "customData": {"x": 1}, "seed": 5}
    )
    assert out == {"text": "edited", "seed": 5, "isDeleted": False}


def test_survives_jsonb_round_trip():
    el = normalize("mark", BROWSER_FREEDRAW, "row4")
    assert json.loads(json.dumps(el)) == el


if __name__ == "__main__":
    failures = []
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
            print(f"  PASS  {name}")
        except AssertionError as e:
            print(f"  FAIL  {name}: {e}")
            failures.append(name)
    print("\n" + ("ALL PASS" if not failures else f"{len(failures)} FAILED: {failures}"))
    sys.exit(1 if failures else 0)
