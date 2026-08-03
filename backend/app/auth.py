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

import hmac
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


# --- Server-to-server auth for AI agents -----------------------------------

S2S_SECRET_HEADER = "x-wb-s2s-secret"
S2S_ACTOR_HEADER = "x-wb-actor-did"


def validate_s2s(secret: str | None, actor_did: str | None) -> dict[str, str]:
    """Authenticate a bsky-mcp server-to-server call acting for `actor_did`.

    AI agents reach the whiteboard through bsky-mcp's wb_* MCP tools. bsky-mcp
    has already resolved which account the call is for (via resolveAccount) but
    holds no bearer token to forward, so it presents a shared secret plus the
    actor's DID.

    Refuses unless WB_S2S_SECRET is configured non-empty — otherwise an
    attacker who could reach the backend directly would authenticate as anyone
    by sending two empty headers. Comparison is constant-time.
    """
    configured = settings.s2s_secret
    if not configured:
        raise AuthError("S2S auth is not enabled (WB_S2S_SECRET unset)")
    if not secret or not hmac.compare_digest(secret, configured):
        raise AuthError("bad S2S secret")
    if not actor_did or not actor_did.startswith("did:"):
        raise AuthError(f"S2S call needs a valid {S2S_ACTOR_HEADER} header")
    return {"did": actor_did, "handle": actor_did}


async def validate_session(token: str) -> dict[str, str] | None:
    """A whiteboard session minted after AT-Proto OAuth login. None if unknown."""
    from . import db
    from .oauth_routes import hash_token

    return await db.get_session(hash_token(token))


async def authenticate(
    bearer: str | None, s2s_secret: str | None, actor_did: str | None
) -> dict[str, str]:
    """Resolve a caller's identity by whichever mechanism they presented.

    Order matters:
      1. S2S — only when the secret header is actually present, so a browser's
         bearer token is never shadowed by it.
      2. A whiteboard session token from AT-Proto OAuth. This is the real
         per-user identity and is checked before the legacy path.
      3. A bsky-mcp access token, if still enabled. Every such token resolves
         to bsky-mcp's default account, so this cannot distinguish users — it
         exists only so agent tokens keep working during the migration.
    """
    if s2s_secret is not None:
        return validate_s2s(s2s_secret, actor_did)
    if bearer:
        session = await validate_session(bearer)
        if session is not None:
            return session
        if settings.allow_bsky_mcp_tokens:
            return await validate_token(bearer)
        raise AuthError("unknown session — sign in with your theblueai.org account")
    raise AuthError("no credentials presented")
