"""AT Protocol OAuth — used purely as an identity provider.

Adapted from Bluesky's official reference implementation
(github.com/bluesky-social/cookbook, python-oauth-web-app), ported from
requests/Flask to httpx/async and reduced to what the whiteboard actually needs.

WHY THIS EXISTS
---------------
The whiteboard previously identified users by a bsky-mcp access token. Those
tokens carry no account binding — `mcp_tokens` has no `did` column — so
`bsky_whoami` fell through to bsky-mcp's DEFAULT_ACCOUNT and *every* user was
resolved as bob.pds.theblueai.org. The whiteboard could not tell two people
apart. WHITEBOARD_PLAN.md always specified AT-Proto accounts on
pds.theblueai.org as the login layer; the implementation had drifted.

THE BIG SIMPLIFICATION
----------------------
The whiteboard never acts on a user's behalf against their PDS. It only needs
to know who they are. So we run the OAuth flow once, verify the returned `sub`,
and then mint our own opaque session. We do not store access/refresh tokens, do
not refresh them, and never make DPoP-signed resource requests. That removes
most of the machinery a general-purpose client needs — the DPoP key here exists
only to satisfy the authorization server during the flow itself.

SECURITY NOTES CARRIED OVER FROM THE REFERENCE
----------------------------------------------
- Every URL derived from user input (handle -> DID doc -> PDS -> auth server)
  is untrusted. `is_safe_url` is a partial SSRF mitigation; requests are also
  made with redirects disabled and short timeouts.
- Handle resolution is verified bidirectionally: handle -> DID, then the DID
  document must claim that same handle back.
- The `sub` returned by the token endpoint MUST be checked against the DID the
  flow was started for. Callers are responsible for that; see oauth_routes.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any
from urllib.parse import urlparse

import httpx
from authlib.common.security import generate_token
from authlib.jose import JsonWebKey, jwt
from authlib.oauth2.rfc7636 import create_s256_code_challenge

HANDLE_REGEX = (
    r"^([a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)+"
    r"[a-zA-Z]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?$"
)
DID_REGEX = r"^did:[a-z]+:[a-zA-Z0-9._:%-]*[a-zA-Z0-9._-]$"

# Redirects disabled: an open redirect on an untrusted host would defeat the
# SSRF check we just performed on the original URL.
_HTTP_KW: dict[str, Any] = {
    "timeout": httpx.Timeout(10.0, connect=5.0),
    "follow_redirects": False,
    "headers": {"User-Agent": "whiteboard.theblueai.org OAuth client"},
}


class OAuthError(Exception):
    pass


# --- validation -------------------------------------------------------------


def is_valid_handle(handle: str) -> bool:
    return re.match(HANDLE_REGEX, handle) is not None


def is_valid_did(did: str) -> bool:
    return re.match(DID_REGEX, did) is not None


def is_safe_url(url: str) -> bool:
    """Partial SSRF mitigation. Not a complete defence on its own.

    Rejects anything that isn't a plain https:// URL to a public-looking
    hostname — no ports, no credentials, no internal TLDs, no bare IPs.
    """
    parts = urlparse(url)
    if not (
        parts.scheme == "https"
        and parts.hostname is not None
        and parts.hostname == parts.netloc
        and parts.username is None
        and parts.password is None
        and parts.port is None
    ):
        return False

    segments = parts.hostname.split(".")
    if len(segments) < 2 or segments[-1] in ("local", "arpa", "internal", "localhost"):
        return False
    # A numeric TLD means this is an IP address, not a name.
    return not segments[-1].isdigit()


def _require_safe(url: str) -> None:
    if not is_safe_url(url):
        raise OAuthError(f"refusing to fetch unsafe URL: {url}")


# --- identity resolution ----------------------------------------------------


def handle_from_doc(doc: dict) -> str | None:
    for aka in doc.get("alsoKnownAs", []):
        if aka.startswith("at://"):
            handle = aka[5:]
            if is_valid_handle(handle):
                return handle
    return None


def _resolve_handle_dns(handle: str) -> str | None:
    """DNS TXT at _atproto.<handle>. Blocking; call via asyncio.to_thread.

    Optional — dnspython may not be installed, and many handles (including
    every account on our PDS) publish the HTTPS well-known instead.
    """
    try:
        import dns.resolver
    except ImportError:
        return None
    try:
        for record in dns.resolver.resolve(f"_atproto.{handle}", "TXT"):
            val = record.to_text().replace('"', "")
            if val.startswith("did="):
                did = val[4:]
                if is_valid_did(did):
                    return did
    except Exception:
        return None
    return None


async def resolve_handle(handle: str) -> str | None:
    """handle -> DID, via DNS TXT then the HTTPS well-known."""
    if not is_valid_handle(handle):
        return None

    did = await asyncio.to_thread(_resolve_handle_dns, handle)
    if did:
        return did

    # NOTE: `handle` is untrusted input and becomes a hostname here.
    url = f"https://{handle}/.well-known/atproto-did"
    if not is_safe_url(url):
        return None
    try:
        async with httpx.AsyncClient(**_HTTP_KW) as client:
            resp = await client.get(url)
    except httpx.HTTPError:
        return None
    if resp.status_code != 200:
        return None
    candidate = resp.text.split()[0] if resp.text.split() else ""
    return candidate if is_valid_did(candidate) else None


async def resolve_did(did: str) -> dict | None:
    """DID -> DID document."""
    if not is_valid_did(did):
        return None
    try:
        async with httpx.AsyncClient(**_HTTP_KW) as client:
            if did.startswith("did:plc:"):
                resp = await client.get(f"https://plc.directory/{did}")
            elif did.startswith("did:web:"):
                domain = did[8:]
                if not is_valid_handle(domain):
                    return None
                url = f"https://{domain}/.well-known/did.json"
                _require_safe(url)
                resp = await client.get(url)
            else:
                raise OAuthError(f"unsupported DID method: {did}")
    except httpx.HTTPError:
        return None
    if resp.status_code != 200:
        return None
    return resp.json()


async def resolve_identity(atid: str) -> tuple[str, str, dict]:
    """Resolve a handle or DID to (did, handle, did_document).

    The handle is verified bidirectionally: resolving handle->DID is not enough,
    the DID document must claim the same handle back. Otherwise anyone could
    point a DNS record at someone else's DID and impersonate them.
    """
    if is_valid_handle(atid):
        handle = atid
        did = await resolve_handle(handle)
        if not did:
            raise OAuthError(f"could not resolve handle: {handle}")
        doc = await resolve_did(did)
        if not doc:
            raise OAuthError(f"could not resolve DID: {did}")
        if handle_from_doc(doc) != handle:
            raise OAuthError(f"DID document does not claim handle: {handle}")
        return did, handle, doc

    if is_valid_did(atid):
        did = atid
        doc = await resolve_did(did)
        if not doc:
            raise OAuthError(f"could not resolve DID: {did}")
        handle = handle_from_doc(doc)
        if not handle:
            raise OAuthError(f"DID document declares no handle: {did}")
        if await resolve_handle(handle) != did:
            raise OAuthError(f"handle does not resolve back to DID: {handle}")
        return did, handle, doc

    raise OAuthError(f"not a handle or DID: {atid}")


def pds_endpoint(doc: dict) -> str:
    for svc in doc.get("service", []):
        if svc.get("id") == "#atproto_pds":
            return svc["serviceEndpoint"]
    raise OAuthError("no #atproto_pds service in DID document")


# --- authorization server discovery -----------------------------------------


def is_valid_authserver_meta(obj: dict, url: str) -> bool:
    """Check auth server metadata against the atproto OAuth profile."""
    fetch_url = urlparse(url)
    issuer_url = urlparse(obj["issuer"])
    checks = [
        (issuer_url.hostname == fetch_url.hostname, "issuer hostname mismatch"),
        (issuer_url.scheme == "https", "issuer must be https"),
        (issuer_url.port is None, "issuer must not specify a port"),
        (issuer_url.path in ("", "/"), "issuer must have no path"),
        (issuer_url.params == "" and issuer_url.fragment == "", "issuer must be bare"),
        ("code" in obj["response_types_supported"], "no code response type"),
        ("authorization_code" in obj["grant_types_supported"], "no authorization_code grant"),
        ("S256" in obj["code_challenge_methods_supported"], "no S256 PKCE"),
        ("private_key_jwt" in obj["token_endpoint_auth_methods_supported"],
         "no private_key_jwt"),
        ("ES256" in obj["token_endpoint_auth_signing_alg_values_supported"],
         "no ES256 client assertion"),
        ("atproto" in obj["scopes_supported"], "no atproto scope"),
        (obj["authorization_response_iss_parameter_supported"] is True, "no iss parameter"),
        (obj.get("pushed_authorization_request_endpoint") is not None, "no PAR endpoint"),
        (obj["require_pushed_authorization_requests"] is True, "PAR not required"),
        ("ES256" in obj["dpop_signing_alg_values_supported"], "no ES256 DPoP"),
        (obj["client_id_metadata_document_supported"] is True,
         "client metadata documents unsupported"),
    ]
    for ok, why in checks:
        if not ok:
            raise OAuthError(f"authorization server metadata invalid: {why}")
    return True


async def resolve_pds_authserver(pds_url: str) -> str:
    """PDS (resource server) -> its authorization server origin."""
    _require_safe(pds_url)
    async with httpx.AsyncClient(**_HTTP_KW) as client:
        resp = await client.get(f"{pds_url}/.well-known/oauth-protected-resource")
    if resp.status_code != 200:
        raise OAuthError(f"oauth-protected-resource returned {resp.status_code}")
    servers = resp.json().get("authorization_servers") or []
    if not servers:
        raise OAuthError("PDS advertises no authorization server")
    return servers[0]


async def fetch_authserver_meta(authserver_url: str) -> dict:
    _require_safe(authserver_url)
    async with httpx.AsyncClient(**_HTTP_KW) as client:
        resp = await client.get(f"{authserver_url}/.well-known/oauth-authorization-server")
    if resp.status_code != 200:
        raise OAuthError(f"authorization server metadata returned {resp.status_code}")
    meta = resp.json()
    is_valid_authserver_meta(meta, authserver_url)
    return meta


# --- JWTs: client assertion + DPoP proof ------------------------------------


def client_assertion_jwt(client_id: str, authserver_url: str, client_jwk: Any) -> str:
    """Self-signed JWT proving we hold the key published in our JWKS."""
    now = int(time.time())
    return jwt.encode(
        {"alg": "ES256", "kid": client_jwk["kid"]},
        {
            "iss": client_id,
            "sub": client_id,
            "aud": authserver_url,
            "jti": generate_token(),
            "iat": now,
            "exp": now + 60,
        },
        client_jwk,
    ).decode("utf-8")


def authserver_dpop_jwt(method: str, url: str, nonce: str, dpop_jwk: Any) -> str:
    """DPoP proof binding this request to our ephemeral key.

    `htu` must be the URL WITHOUT query parameters — @atproto/oauth-client
    shipped a bug including them, fixed in 0.3.18. The URLs we pass here are
    bare endpoints, but don't add query strings to them.
    """
    now = int(time.time())
    body: dict[str, Any] = {
        "jti": generate_token(),
        "htm": method,
        "htu": url,
        "iat": now,
        "exp": now + 30,
    }
    if nonce:
        body["nonce"] = nonce
    return jwt.encode(
        {
            "typ": "dpop+jwt",
            "alg": "ES256",
            "jwk": json.loads(dpop_jwk.as_json(is_private=False)),
        },
        body,
        dpop_jwk,
    ).decode("utf-8")


def _is_use_dpop_nonce(resp: httpx.Response) -> bool:
    if resp.status_code not in (400, 401):
        return False
    try:
        if resp.json().get("error") == "use_dpop_nonce":
            return True
    except Exception:
        pass
    return "use_dpop_nonce" in resp.headers.get("WWW-Authenticate", "")


async def auth_server_post(
    authserver_url: str,
    client_id: str,
    client_jwk: Any,
    dpop_jwk: Any,
    dpop_nonce: str,
    post_url: str,
    post_data: dict,
) -> tuple[str, httpx.Response]:
    """POST to the auth server with client assertion + DPoP, retrying once on
    a nonce challenge (the server issues nonces lazily, so the first call of a
    flow is expected to fail this way)."""
    _require_safe(post_url)
    payload = dict(post_data)
    payload.update(
        {
            "client_id": client_id,
            "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
            "client_assertion": client_assertion_jwt(client_id, authserver_url, client_jwk),
        }
    )

    async with httpx.AsyncClient(**_HTTP_KW) as client:
        proof = authserver_dpop_jwt("POST", post_url, dpop_nonce, dpop_jwk)
        resp = await client.post(post_url, data=payload, headers={"DPoP": proof})

        if _is_use_dpop_nonce(resp):
            dpop_nonce = resp.headers.get("DPoP-Nonce", "")
            proof = authserver_dpop_jwt("POST", post_url, dpop_nonce, dpop_jwk)
            resp = await client.post(post_url, data=payload, headers={"DPoP": proof})

    return dpop_nonce, resp


# --- the flow ---------------------------------------------------------------


async def send_par_auth_request(
    authserver_url: str,
    authserver_meta: dict,
    login_hint: str,
    client_id: str,
    redirect_uri: str,
    scope: str,
    client_jwk: Any,
    dpop_jwk: Any,
) -> tuple[str, str, str, httpx.Response]:
    """Pushed Authorization Request. Returns (pkce_verifier, state, nonce, resp)."""
    par_url = authserver_meta["pushed_authorization_request_endpoint"]
    state = generate_token()
    pkce_verifier = generate_token(48)

    body = {
        "response_type": "code",
        "code_challenge": create_s256_code_challenge(pkce_verifier),
        "code_challenge_method": "S256",
        "state": state,
        "redirect_uri": redirect_uri,
        "scope": scope,
    }
    if login_hint:
        body["login_hint"] = login_hint

    dpop_nonce, resp = await auth_server_post(
        authserver_url=authserver_url,
        client_id=client_id,
        client_jwk=client_jwk,
        dpop_jwk=dpop_jwk,
        dpop_nonce="",  # not yet known; the server will challenge us
        post_url=par_url,
        post_data=body,
    )
    return pkce_verifier, state, dpop_nonce, resp


async def initial_token_request(
    auth_request: dict,
    code: str,
    client_id: str,
    redirect_uri: str,
    client_jwk: Any,
) -> tuple[dict, str]:
    """Exchange the authorization code for tokens.

    IMPORTANT: the caller MUST verify `sub` in the returned body against the DID
    this flow was started for. Without that check, an authorization server (or
    anyone who can complete a flow) could hand back a token for a different
    account and we would accept it.
    """
    authserver_url = auth_request["authserver_iss"]
    meta = await fetch_authserver_meta(authserver_url)
    token_url = meta["token_endpoint"]

    dpop_jwk = JsonWebKey.import_key(json.loads(auth_request["dpop_private_jwk"]))

    dpop_nonce, resp = await auth_server_post(
        authserver_url=authserver_url,
        client_id=client_id,
        client_jwk=client_jwk,
        dpop_jwk=dpop_jwk,
        dpop_nonce=auth_request.get("dpop_authserver_nonce", ""),
        post_url=token_url,
        post_data={
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": auth_request["pkce_verifier"],
        },
    )
    if resp.status_code != 200:
        raise OAuthError(f"token request failed ({resp.status_code}): {resp.text[:300]}")
    return resp.json(), dpop_nonce


def new_dpop_key() -> Any:
    """A fresh ES256 key, one per authorization flow."""
    return JsonWebKey.generate_key("EC", "P-256", is_private=True)
