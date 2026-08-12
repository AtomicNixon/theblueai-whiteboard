"""Server-side rasterizer: element list -> PNG bytes.

Built for one purpose: an agent that can only read `snapshot`'s element JSON
has no way to see what it drew, only what it specified. A browser closes that
loop for free (paint the SVG, look at the screen); this endpoint closes it for
everyone else. It is not a faithful Excalidraw renderer — no roughness, no
hand-drawn jitter, no font metrics — just enough geometry to tell a cat from
a house.
"""
from __future__ import annotations

from typing import Any

from PIL import Image, ImageDraw, ImageFont

_PADDING = 40
_MAX_SIDE = 4000  # refuse to rasterize a canvas someone stretched to absurdity


def _bbox(elements: list[dict[str, Any]]) -> tuple[float, float, float, float]:
    if not elements:
        return (0.0, 0.0, 100.0, 100.0)
    xs0 = [e.get("x", 0.0) for e in elements]
    ys0 = [e.get("y", 0.0) for e in elements]
    xs1 = [e.get("x", 0.0) + e.get("width", 0.0) for e in elements]
    ys1 = [e.get("y", 0.0) + e.get("height", 0.0) for e in elements]
    return (min(xs0), min(ys0), max(xs1), max(ys1))


def _font(size: int) -> ImageFont.ImageFont:
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        # Pillow < 10.1 load_default doesn't take a size argument.
        return ImageFont.load_default()


def render_elements(elements: list[dict[str, Any]], background: str = "#ffffff") -> bytes:
    """Rasterize a snapshot's elements to a PNG, cropped to their bounding box."""
    visible = [e for e in elements if e.get("type") != "image" and not e.get("isDeleted")]

    x0, y0, x1, y1 = _bbox(visible)
    width = min(max(int(x1 - x0) + 2 * _PADDING, 1), _MAX_SIDE)
    height = min(max(int(y1 - y0) + 2 * _PADDING, 1), _MAX_SIDE)

    img = Image.new("RGB", (width, height), background)
    draw = ImageDraw.Draw(img)

    def ox(x: float) -> float:
        return x - x0 + _PADDING

    def oy(y: float) -> float:
        return y - y0 + _PADDING

    for el in visible:
        etype = el.get("type")
        stroke = el.get("strokeColor") or "#1e1e1e"
        fill = el.get("backgroundColor") or "transparent"
        width_px = max(int(el.get("strokeWidth") or 2), 1)
        ex, ey = el.get("x", 0.0), el.get("y", 0.0)
        ew, eh = el.get("width", 0.0), el.get("height", 0.0)

        if etype == "rectangle":
            box = [ox(ex), oy(ey), ox(ex + ew), oy(ey + eh)]
            draw.rectangle(box, outline=stroke, width=width_px,
                            fill=None if fill == "transparent" else fill)

        elif etype == "ellipse":
            box = [ox(ex), oy(ey), ox(ex + ew), oy(ey + eh)]
            draw.ellipse(box, outline=stroke, width=width_px,
                          fill=None if fill == "transparent" else fill)

        elif etype == "diamond":
            pts = [
                (ox(ex + ew / 2), oy(ey)),
                (ox(ex + ew), oy(ey + eh / 2)),
                (ox(ex + ew / 2), oy(ey + eh)),
                (ox(ex), oy(ey + eh / 2)),
            ]
            draw.polygon(pts, outline=stroke, width=width_px,
                         fill=None if fill == "transparent" else fill)

        elif etype in ("line", "arrow", "freedraw"):
            points = el.get("points") or [[0, 0], [ew, eh]]
            pts = [(ox(ex + p[0]), oy(ey + p[1])) for p in points]
            if len(pts) >= 2:
                draw.line(pts, fill=stroke, width=width_px, joint="curve")
            elif len(pts) == 1:
                r = width_px / 2
                px, py = pts[0]
                draw.ellipse([px - r, py - r, px + r, py + r], fill=stroke)

        elif etype == "text":
            text = el.get("text") or ""
            size = int(el.get("fontSize") or 20)
            font = _font(size)
            draw.multiline_text((ox(ex), oy(ey)), text, fill=stroke, font=font)

    buf = _png_bytes(img)
    return buf


def _png_bytes(img: Image.Image) -> bytes:
    import io
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()
