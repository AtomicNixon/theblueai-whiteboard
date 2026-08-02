"""AI trigger — wake a tagged AI by posting a Bluesky mention via bsky-mcp.

Implements option 1 (Bob's choice): when a user tags an AI (@handle) on a
whiteboard canvas, the whiteboard posts a Bluesky post mentioning that AI.
The post carries the canvas_id; the tagged AI's next bsky_read_queue surfaces
the mention, and the AI calls wb_read_canvas to look at the canvas.

This is the "check your mentions" model — async, no live wake, no new
infrastructure. Latency = however long until a session of the tagged AI runs.
The tag is persisted on the canvas regardless; only the wake is best-effort.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re

import httpx

from .config import settings

log = logging.getLogger("whiteboard.ai_trigger")

# Matches @handle (at least one dot, so "bob.pds.theblueai.org" matches but
# a bare "@bob" doesn't — ATProto handles always have a domain).
TAG_RE = re.compile(r"@([a-z0-9._-]+(?:\.[a-z0-9._-]+)+)", re.IGNORECASE)


def extract_tags(text: str) -> list[str]:
    """Return list of handles mentioned via @ in the given text."""
    return TAG_RE.findall(text)


def schedule_ai_tagged(canvas_id: str, tag: str) -> None:
    """Fire the (best-effort) AI wake in the background so it never blocks the
    element-create request. Exceptions are logged, never propagated."""
    asyncio.create_task(_notify_ai_tagged_guarded(canvas_id, tag))


async def _notify_ai_tagged_guarded(canvas_id: str, tag: str) -> None:
    try:
        await notify_ai_tagged(canvas_id, tag)
    except Exception:
        log.exception("AI_TAG_WAKE_UNEXPECTED canvas=%s tag=%s", canvas_id, tag)


async def notify_ai_tagged(canvas_id: str, tag: str) -> None:
    """Post a Bluesky mention via bsky-mcp so the tagged AI's next
    bsky_read_queue surfaces it.

    Posts as the configured waker account (WB_WAKER_ACCOUNT) using a bsky-mcp
    access token (WB_WAKER_BSKY_TOKEN). If either is unset, logs and returns —
    the tag is still on the canvas, just no wake is sent.

    The mention text includes the canvas_id (8-char prefix for readability)
    and a link to the canvas. The AI reads the mention, extracts the canvas_id,
    and calls wb_read_canvas.
    """
    token = settings.waker_bsky_token
    if not token:
        log.info("AI_TAG_SKIP canvas=%s tag=%s (no WB_WAKER_BSKY_TOKEN set)", canvas_id, tag)
        return

    short_id = canvas_id[:8]
    canvas_url = f"{settings.public_url}/c/{canvas_id}"
    text = f"@{tag} you're tagged on whiteboard canvas {short_id} — take a look: {canvas_url}"

    payload = {
        "jsonrpc": "2.0",
        "method": "tools/call",
        "params": {
            "name": "bsky_post",
            "arguments": {
                "text": text,
                "account": settings.waker_account,
            },
        },
        "id": 1,
    }
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            # MCP requires an initialize handshake before tool calls in a
            # session-based transport. bsky-mcp uses stateless mode (new
            # transport per request), so a bare tools/call works.
            resp = await client.post(
                f"{settings.bsky_mcp_url}/mcp",
                headers=headers,
                json=payload,
            )
        if resp.status_code >= 400:
            log.error("AI_TAG_WAKE_FAILED canvas=%s tag=%s status=%s body=%s",
                      canvas_id, tag, resp.status_code, resp.text[:200])
            return
        # MCP success: check for tool error in the result.
        data = resp.json()
        result = data.get("result", {})
        if result.get("isError"):
            log.error("AI_TAG_WAKE_TOOL_ERROR canvas=%s tag=%s result=%s",
                      canvas_id, tag, json.dumps(result)[:200])
            return
        log.info("AI_TAG_WAKE_SENT canvas=%s tag=%s (mention posted)", canvas_id, tag)
    except httpx.HTTPError as e:
        log.error("AI_TAG_WAKE_ERROR canvas=%s tag=%s err=%s", canvas_id, tag, e)
