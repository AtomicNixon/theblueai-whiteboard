"""Auth: validates a user's session against bsky-mcp.

The whiteboard reuses bsky-mcp's OAuth layer. A client presents a bsky-mcp
access token (Bearer); we ask bsky-mcp's /mcp endpoint (or a dedicated
whoami call) to confirm the token is valid and return the caller's DID +
handle. We do NOT issue tokens ourselves.

For v1 we validate by calling bsky-mcp's `bsky_whoami` MCP tool with the
caller's token. If bsky-mcp exposes a lighter-weight token-introspect
endpoint later, swap this out.
"""
from __future__ import annotations

import json
import time
from typing import Any

import httpx

from .config import settings


class AuthError(Exception):
    pass


def _parse_sse_json(body: str) -> dict[str, Any]:
    """Extract the JSON-RPC payload from an SSE-framed response body.

    SSE frames look like:
        event: message
        data: {"jsonrpc":"2.0", ...}
        <blank line>

    A single event's data can span multiple `data:` lines (joined by SSE
    spec), and a stream can contain multiple events — we take the first
    complete one, which is what a single request/response MCP call sends.
    """
    data_lines: list[str] = []
    for line in body.splitlines():
        if line.startswith("data:"):
            data_lines.append(line[len("data:"):].lstrip(" "))
        elif data_lines and line.strip() == "":
            break  # end of first event
    if not data_lines:
        raise AuthError(f"no SSE data field in bsky-mcp response: {body[:200]!r}")
    try:
        return json.loads("\n".join(data_lines))
    except json.JSONDecodeError as e:
        raise AuthError(f"unparseable SSE data field: {e}") from e


class BskyMcpClient:
    """Thin client over bsky-mcp's MCP endpoint for token validation + tool calls."""

    def __init__(self, base_url: str | None = None) -> None:
        self.base_url = (base_url or settings.bsky_mcp_url).rstrip("/")

    async def whoami(self, access_token: str) -> dict[str, Any]:
        """Validate the bearer token via bsky-mcp and return {did, handle}.

        Calls the MCP `initialize` + `tools/call bsky_whoami` flow. Raises
        AuthError on any failure (invalid token, bsky-mcp down, etc.).
        """
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        async with httpx.AsyncClient(timeout=15.0) as client:
            try:
                resp = await client.post(
                    f"{self.base_url}/mcp",
                    headers=headers,
                    json={
                        "jsonrpc": "2.0",
                        "method": "initialize",
                        "params": {
                            "protocolVersion": "2025-06-18",
                            "capabilities": {},
                            "clientInfo": {"name": "whiteboard", "version": "0.1.0"},
                        },
                        "id": 1,
                    },
                )
                if resp.status_code == 401:
                    raise AuthError("invalid or expired bsky-mcp token")
                resp.raise_for_status()
            except httpx.HTTPError as e:
                raise AuthError(f"bsky-mcp unreachable: {e}") from e

            # Call bsky_whoami to resolve the caller's identity.
            try:
                resp = await client.post(
                    f"{self.base_url}/mcp",
                    headers=headers,
                    json={
                        "jsonrpc": "2.0",
                        "method": "tools/call",
                        "params": {"name": "bsky_whoami", "arguments": {}},
                        "id": 2,
                    },
                )
                resp.raise_for_status()
            except httpx.HTTPError as e:
                raise AuthError(f"bsky_cp whoami failed: {e}") from e

        # MCP tool results come back as content blocks. bsky-mcp may respond
        # with plain JSON or with SSE framing (content-type: text/event-stream,
        # body like "event: message\ndata: {...}\n\n") depending on what the
        # Accept header offers — we advertise text/event-stream above, and the
        # server takes us up on it. Handle both.
        content_type = resp.headers.get("content-type", "")
        if "text/event-stream" in content_type:
            payload = _parse_sse_json(resp.text)
        else:
            payload = resp.json()
        result = payload.get("result", {})
        content = result.get("content", [])
        if not content:
            raise AuthError("whoami returned no content")
        text = content[0].get("text", "{}")
        try:
            who = json.loads(text)
        except json.JSONDecodeError as e:
            raise AuthError(f"whoami returned unparseable content: {e}") from e
        did = who.get("did")
        handle = who.get("handle")
        if not did:
            raise AuthError("whoami did not return a DID")
        return {"did": did, "handle": handle or did}


bsky = BskyMcpClient()

# Each whoami costs two bsky-mcp round-trips; cache by token with a short TTL
# so repeated requests (snapshot, element ops) don't re-validate every time.
_AUTH_CACHE_TTL_S = 300
_token_cache: dict[str, tuple[float, dict[str, str]]] = {}


async def validate_token(access_token: str) -> dict[str, str]:
    """Return {did, handle} for a valid bsky-mcp bearer token, else AuthError."""
    now = time.monotonic()
    cached = _token_cache.get(access_token)
    if cached is not None and now - cached[0] < _AUTH_CACHE_TTL_S:
        return cached[1]
    who = await bsky.whoami(access_token)
    _token_cache[access_token] = (now, who)
    return who
