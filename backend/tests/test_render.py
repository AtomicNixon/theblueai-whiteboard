"""GET /canvases/{id}/render — server-side rasterization.

Closes the loop an agent otherwise can't: `snapshot` hands back the element
JSON it posted, never what it looks like painted. This endpoint is that
missing render step.
"""
from __future__ import annotations

import io

from PIL import Image


def _post(client, headers, canvas_id, kind, data):
    r = client.post(f"/api/canvases/{canvas_id}/elements", headers=headers,
                     json={"kind": kind, "data": data})
    assert r.status_code == 200, r.text
    return r.json()


def test_empty_canvas_renders_a_blank_image(client, alice_headers, canvas):
    r = client.get(f"/api/canvases/{canvas['id']}/render", headers=alice_headers)
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    img = Image.open(io.BytesIO(r.content))
    assert img.format == "PNG"
    assert img.size[0] > 0 and img.size[1] > 0


def test_render_reflects_posted_shape(client, alice_headers, canvas):
    _post(client, alice_headers, canvas["id"], "mark", {
        "type": "rectangle", "x": 10.0, "y": 10.0, "width": 80.0, "height": 40.0,
        "strokeColor": "#ff0000", "backgroundColor": "#ff0000", "fillStyle": "solid",
    })
    r = client.get(f"/api/canvases/{canvas['id']}/render", headers=alice_headers)
    assert r.status_code == 200
    img = Image.open(io.BytesIO(r.content)).convert("RGB")
    # Somewhere inside the shape's bounding box there should be red pixels —
    # the point isn't pixel-perfect placement, it's that drawing something
    # produces a visibly different image from drawing nothing.
    colors = {img.getpixel((x, y)) for x in range(0, img.size[0], 4)
              for y in range(0, img.size[1], 4)}
    assert any(r_ > 180 and g < 100 and b < 100 for r_, g, b in colors)


def test_render_requires_auth(client, canvas):
    r = client.get(f"/api/canvases/{canvas['id']}/render")
    assert r.status_code == 401


def test_render_missing_canvas_404s(client, alice_headers):
    r = client.get("/api/canvases/does-not-exist/render", headers=alice_headers)
    assert r.status_code == 404
