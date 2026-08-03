"""AT Protocol OAuth endpoints — sign in with your theblueai.org account.

Flow:
  1. POST /api/auth/login {handle}
       resolve handle -> DID -> PDS -> authorization server, run a Pushed
       Authorization Request, store the flow state, return a redirect URL.
  2. Browser visits the PDS authorization page and approves.
  3. GET /oauth/callback?code&state&iss
       consume the single-use flow state, exchange the code, VERIFY the
       returned `sub` matches the DID we started for, mint a whiteboard
       session, redirect back to the SPA with the token in the URL fragment.

The session is ours, not the PDS's. We never store or refresh AT-Proto tokens
because the whiteboard never acts on a user's behalf against their PDS — it
only needs to know who they are. See atproto_oauth.py for the fuller rationale.
"""
from __future__ import annotations

import hashlib
import json
import logging
import secrets
from typing import Annotated
from urllib.parse import urlencode

from authlib.jose import JsonWebKey
from fastapi import APIRouter, Header, HTTPException, status
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel

from . import atproto_oauth as oa
from . import db
from .config import settings

log = logging.getLogger("whiteboard.oauth")
router = APIRouter()

# Only identity is needed. `transition:generic` would grant repo write access
# we have no use for, and asking for it would make the consent screen alarming
# for no reason.
OAUTH_SCOPE = "atproto"


def client_id() -> str:
    return f"{settings.public_url}/oauth/client-metadata.json"


def redirect_uri() -> str:
    return f"{settings.public_url}/oauth/callback"


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _generate_client_jwk() -> dict:
    key = JsonWebKey.generate_key("EC", "P-256", is_private=True)
    jwk = json.loads(key.as_json(is_private=True))
    jwk["kid"] = secrets.token_hex(8)
    jwk["use"] = "sig"
    jwk["alg"] = "ES256"
    return jwk


async def _client_key():
    return JsonWebKey.import_key(await db.get_or_create_client_jwk(_generate_client_jwk))


# --- client metadata (client_id IS this document's URL) ---------------------


@router.get("/oauth/client-metadata.json")
async def client_metadata() -> JSONResponse:
    return JSONResponse(
        {
            "client_id": client_id(),
            "client_name": "BlueAI Whiteboard",
            "client_uri": settings.public_url,
            "redirect_uris": [redirect_uri()],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "scope": OAUTH_SCOPE,
            "token_endpoint_auth_method": "private_key_jwt",
            "token_endpoint_auth_signing_alg": "ES256",
            "dpop_bound_access_tokens": True,
            "application_type": "web",
            "jwks_uri": f"{settings.public_url}/oauth/jwks.json",
        }
    )


@router.get("/oauth/jwks.json")
async def jwks() -> JSONResponse:
    """Public half of our client key. Never expose the private key here."""
    private = await db.get_or_create_client_jwk(_generate_client_jwk)
    public = {k: v for k, v in private.items() if k != "d"}
    return JSONResponse({"keys": [public]})


# --- login ------------------------------------------------------------------


class LoginIn(BaseModel):
    handle: str


@router.post("/api/auth/login")
async def login(body: LoginIn) -> dict:
    handle = body.handle.strip().lstrip("@").lower()
    if not handle:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "handle is required")

    try:
        did, resolved_handle, doc = await oa.resolve_identity(handle)
        pds_url = oa.pds_endpoint(doc)
        authserver_url = await oa.resolve_pds_authserver(pds_url)
        authserver_meta = await oa.fetch_authserver_meta(authserver_url)
    except oa.OAuthError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e

    dpop_key = oa.new_dpop_key()
    try:
        pkce_verifier, state, dpop_nonce, resp = await oa.send_par_auth_request(
            authserver_url=authserver_url,
            authserver_meta=authserver_meta,
            login_hint=resolved_handle,
            client_id=client_id(),
            redirect_uri=redirect_uri(),
            scope=OAUTH_SCOPE,
            client_jwk=await _client_key(),
            dpop_jwk=dpop_key,
        )
    except oa.OAuthError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(e)) from e

    if resp.status_code not in (200, 201):
        log.warning("PAR failed for %s: %s %s", handle, resp.status_code, resp.text[:300])
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            f"authorization server rejected the request ({resp.status_code})",
        )

    request_uri = resp.json()["request_uri"]

    await db.save_auth_request(
        {
            "state": state,
            "authserver_iss": authserver_meta["issuer"],
            "did": did,
            "handle": resolved_handle,
            "pds_url": pds_url,
            "pkce_verifier": pkce_verifier,
            "dpop_private_jwk": dpop_key.as_json(is_private=True),
            "dpop_authserver_nonce": dpop_nonce,
        }
    )
    await db.prune_auth_requests()

    params = urlencode({"client_id": client_id(), "request_uri": request_uri})
    auth_url = f"{authserver_meta['authorization_endpoint']}?{params}"
    log.info("AUTH_START handle=%s did=%s", resolved_handle, did)
    return {"redirect_url": auth_url, "handle": resolved_handle, "did": did}


# --- callback ---------------------------------------------------------------


def _fail(reason: str) -> RedirectResponse:
    """Bounce back to the SPA with an error rather than showing raw JSON."""
    log.warning("AUTH_FAIL %s", reason)
    return RedirectResponse(f"{settings.public_url}/#error={urlencode({'e': reason})[2:]}",
                            status_code=status.HTTP_302_FOUND)


@router.get("/oauth/callback")
async def callback(state: str = "", code: str = "", iss: str = "",
                   error: str = "", error_description: str = ""):
    if error:
        return _fail(f"{error}: {error_description}"[:200])
    if not state or not code:
        return _fail("missing state or code")

    auth_request = await db.take_auth_request(state)  # single-use
    if auth_request is None:
        return _fail("unknown or already-used state")

    # The issuer must match the server we sent the user to; otherwise a
    # different authorization server could complete someone else's flow.
    if iss and iss != auth_request["authserver_iss"]:
        return _fail("issuer mismatch")

    try:
        tokens, _ = await oa.initial_token_request(
            auth_request=auth_request,
            code=code,
            client_id=client_id(),
            redirect_uri=redirect_uri(),
            client_jwk=await _client_key(),
        )
    except oa.OAuthError as e:
        return _fail(f"token exchange failed: {e}"[:200])

    # THE load-bearing check. Without it the authorization server could return
    # a token for any account and we would happily log the user in as them.
    if tokens.get("sub") != auth_request["did"]:
        return _fail("token subject does not match the requested account")

    token = secrets.token_urlsafe(32)
    await db.create_session(
        hash_token(token), auth_request["did"], auth_request["handle"],
        settings.session_ttl_seconds,
    )
    await db.prune_sessions()
    log.info("AUTH_OK handle=%s did=%s", auth_request["handle"], auth_request["did"])

    # Fragment, not query: fragments are not sent to servers and don't land in
    # access logs or Referer headers.
    return RedirectResponse(f"{settings.public_url}/#session={token}",
                            status_code=status.HTTP_302_FOUND)


# --- session ----------------------------------------------------------------


def _bearer(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        return ""
    return authorization[7:].strip()


@router.get("/api/auth/me")
async def me(authorization: Annotated[str | None, Header()] = None) -> dict:
    token = _bearer(authorization)
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "no session")
    who = await db.get_session(hash_token(token))
    if who is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "session expired or unknown")
    return who


@router.post("/api/auth/logout")
async def logout(authorization: Annotated[str | None, Header()] = None) -> dict:
    token = _bearer(authorization)
    if token:
        await db.delete_session(hash_token(token))
    return {"ok": True}
