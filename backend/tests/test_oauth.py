"""AT Protocol OAuth — login flow, session handling, and the security checks.

The whiteboard uses AT-Proto OAuth purely as an identity provider. That makes
two checks load-bearing, and they get the most attention here:

  1. The `sub` returned by the token endpoint must match the DID the flow was
     started for. Without it, completing any flow would let a caller be logged
     in as somebody else.
  2. Authorization state is single-use. Without that, an intercepted code could
     be replayed.

Everything reachable without the network is tested offline; the handful of
tests that talk to the real PDS are marked `network` and skipped by default.
Run them with:  pytest -m network
"""
from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import atproto_oauth as oa  # noqa: E402
from app import db  # noqa: E402
from app.oauth_routes import hash_token  # noqa: E402

ART_DID = "did:plc:f5nmd2dfjm4johmbbeyoyve3"
ART_HANDLE = "art.pds.theblueai.org"


# --- SSRF guard -------------------------------------------------------------

@pytest.mark.parametrize("url", [
    "http://pds.theblueai.org",              # not https
    "https://localhost/x",                   # single-label
    "https://127.0.0.1/x",                   # bare IP
    "https://192.168.1.5/x",
    "https://pds.theblueai.org:8443/x",      # explicit port
    "https://user:pw@pds.theblueai.org/x",   # credentials
    "https://thing.internal/x",
    "https://thing.local/x",
    "https://thing.localhost/x",
    "https://foo.arpa/x",
    "file:///etc/passwd",
    "",
])
def test_is_safe_url_rejects(url):
    assert oa.is_safe_url(url) is False


@pytest.mark.parametrize("url", [
    "https://pds.theblueai.org",
    "https://pds.theblueai.org/oauth/par",
    "https://bsky.social/xrpc/whatever",
])
def test_is_safe_url_accepts(url):
    assert oa.is_safe_url(url) is True


# --- identifier validation --------------------------------------------------

def test_handle_and_did_validation():
    assert oa.is_valid_handle("art.pds.theblueai.org")
    assert oa.is_valid_handle("bob.bsky.social")
    assert not oa.is_valid_handle("nodots")
    assert not oa.is_valid_handle("")
    assert not oa.is_valid_handle("-bad.example.com")

    assert oa.is_valid_did("did:plc:f5nmd2dfjm4johmbbeyoyve3")
    assert oa.is_valid_did("did:web:example.com")
    assert not oa.is_valid_did("")
    assert not oa.is_valid_did("did:")
    assert not oa.is_valid_did("notadid")


def test_handle_from_doc():
    assert oa.handle_from_doc({"alsoKnownAs": [f"at://{ART_HANDLE}"]}) == ART_HANDLE
    assert oa.handle_from_doc({"alsoKnownAs": []}) is None
    assert oa.handle_from_doc({}) is None
    # A non-at:// aka must not be mistaken for a handle.
    assert oa.handle_from_doc({"alsoKnownAs": ["https://example.com"]}) is None


def test_pds_endpoint():
    doc = {"service": [
        {"id": "#other", "serviceEndpoint": "https://nope.example.com"},
        {"id": "#atproto_pds", "serviceEndpoint": "https://pds.theblueai.org"},
    ]}
    assert oa.pds_endpoint(doc) == "https://pds.theblueai.org"
    with pytest.raises(oa.OAuthError):
        oa.pds_endpoint({"service": []})


# --- authorization server metadata validation -------------------------------

GOOD_META = {
    "issuer": "https://pds.theblueai.org",
    "response_types_supported": ["code"],
    "grant_types_supported": ["authorization_code", "refresh_token"],
    "code_challenge_methods_supported": ["S256"],
    "token_endpoint_auth_methods_supported": ["none", "private_key_jwt"],
    "token_endpoint_auth_signing_alg_values_supported": ["ES256"],
    "scopes_supported": ["atproto", "transition:generic"],
    "authorization_response_iss_parameter_supported": True,
    "pushed_authorization_request_endpoint": "https://pds.theblueai.org/oauth/par",
    "require_pushed_authorization_requests": True,
    "dpop_signing_alg_values_supported": ["ES256"],
    "client_id_metadata_document_supported": True,
}


def test_valid_metadata_accepted():
    assert oa.is_valid_authserver_meta(dict(GOOD_META), "https://pds.theblueai.org")


@pytest.mark.parametrize("mutate,why", [
    ({"issuer": "https://evil.example.com"}, "issuer hostname must match where we fetched it"),
    ({"issuer": "http://pds.theblueai.org"}, "issuer must be https"),
    ({"require_pushed_authorization_requests": False}, "PAR must be required"),
    ({"client_id_metadata_document_supported": False}, "we rely on metadata documents"),
    ({"code_challenge_methods_supported": ["plain"]}, "S256 PKCE required"),
    ({"dpop_signing_alg_values_supported": ["RS256"]}, "ES256 DPoP required"),
    ({"authorization_response_iss_parameter_supported": False}, "iss param required"),
    ({"scopes_supported": ["transition:generic"]}, "atproto scope required"),
])
def test_bad_metadata_rejected(mutate, why):
    meta = dict(GOOD_META)
    meta.update(mutate)
    with pytest.raises((oa.OAuthError, KeyError, AssertionError)), pytest.MonkeyPatch.context():
        oa.is_valid_authserver_meta(meta, "https://pds.theblueai.org")


# --- JWTs -------------------------------------------------------------------

def test_client_assertion_jwt_shape():
    from authlib.jose import JsonWebKey
    from authlib.jose import jwt as jose_jwt

    key = JsonWebKey.generate_key("EC", "P-256", is_private=True)
    raw = json.loads(key.as_json(is_private=True))
    raw["kid"] = "testkid"
    key = JsonWebKey.import_key(raw)

    token = oa.client_assertion_jwt("https://wb/meta.json", "https://pds.theblueai.org", key)
    claims = jose_jwt.decode(token, key)
    assert claims["iss"] == "https://wb/meta.json"
    assert claims["sub"] == "https://wb/meta.json"
    assert claims["aud"] == "https://pds.theblueai.org"
    assert claims["exp"] > claims["iat"]
    assert claims["jti"]


def test_dpop_proof_shape():
    import base64

    key = oa.new_dpop_key()
    proof = oa.authserver_dpop_jwt("POST", "https://pds.theblueai.org/oauth/par", "n0nce", key)
    header_b64 = proof.split(".")[0]
    header = json.loads(base64.urlsafe_b64decode(header_b64 + "=" * (-len(header_b64) % 4)))

    assert header["typ"] == "dpop+jwt"
    assert header["alg"] == "ES256"
    # The public key travels in the header; the private half must not.
    assert "d" not in header["jwk"], "DPoP header must never carry the private key"
    assert header["jwk"]["crv"] == "P-256"

    payload_b64 = proof.split(".")[1]
    payload = json.loads(base64.urlsafe_b64decode(payload_b64 + "=" * (-len(payload_b64) % 4)))
    assert payload["htm"] == "POST"
    assert payload["htu"] == "https://pds.theblueai.org/oauth/par"
    assert "?" not in payload["htu"], "htu must not include a query string"
    assert payload["nonce"] == "n0nce"


def test_dpop_omits_nonce_when_unknown():
    import base64
    proof = oa.authserver_dpop_jwt(
        "POST", "https://pds.theblueai.org/oauth/par", "", oa.new_dpop_key()
    )
    p = proof.split(".")[1]
    payload = json.loads(base64.urlsafe_b64decode(p + "=" * (-len(p) % 4)))
    assert "nonce" not in payload


# --- endpoints --------------------------------------------------------------

def test_client_metadata_document(client):
    r = client.get("/oauth/client-metadata.json")
    assert r.status_code == 200
    m = r.json()
    # client_id must be the URL this very document is served from — that is the
    # whole basis of atproto's client-metadata-document trust model.
    assert m["client_id"] == m["client_uri"] + "/oauth/client-metadata.json"
    assert m["jwks_uri"] == m["client_uri"] + "/oauth/jwks.json"
    assert m["redirect_uris"] == [m["client_uri"] + "/oauth/callback"]
    assert m["token_endpoint_auth_method"] == "private_key_jwt"
    assert m["dpop_bound_access_tokens"] is True
    assert m["scope"] == "atproto", "we only need identity, not repo write access"


def test_jwks_never_exposes_the_private_key(client):
    r = client.get("/oauth/jwks.json")
    assert r.status_code == 200
    keys = r.json()["keys"]
    assert len(keys) == 1
    k = keys[0]
    assert "d" not in k, "private key component leaked in JWKS"
    assert k["kty"] == "EC" and k["crv"] == "P-256" and k["alg"] == "ES256"
    assert k["kid"]


def test_jwks_key_is_stable(client):
    """A key that changed per request would break in-flight authorizations."""
    a = client.get("/oauth/jwks.json").json()["keys"][0]["kid"]
    b = client.get("/oauth/jwks.json").json()["keys"][0]["kid"]
    assert a == b


def test_login_rejects_bad_handles(client):
    for bad in ["", "   ", "nodots", "@", "not a handle"]:
        r = client.post("/api/auth/login", json={"handle": bad})
        assert r.status_code == 400, f"{bad!r} should be rejected"


def test_unknown_api_paths_under_oauth_404(client):
    """/oauth/* must not fall through to the SPA — that's how the PDS ends up
    fetching text/html for our client metadata."""
    r = client.get("/oauth/definitely-not-a-route")
    assert r.status_code == 404


# --- sessions ---------------------------------------------------------------

def test_session_round_trip(client):
    # Run DB calls on the TestClient's own event loop — the connection pool is
    # bound to it, and a fresh asyncio loop would fail with "attached to a
    # different loop".
    call = client.portal.call  # type: ignore[attr-defined]
    token = "test-session-token-abc"
    call(db.create_session, hash_token(token), ART_DID, ART_HANDLE, 3600)

    assert call(db.get_session, hash_token(token)) == {"did": ART_DID, "handle": ART_HANDLE}
    # The raw token must not be what's stored.
    assert call(db.get_session, token) is None

    assert call(db.delete_session, hash_token(token)) is True
    assert call(db.get_session, hash_token(token)) is None


def test_expired_sessions_do_not_authenticate(client):
    call = client.portal.call  # type: ignore[attr-defined]
    token = "already-expired"
    call(db.create_session, hash_token(token), ART_DID, ART_HANDLE, -10)
    assert call(db.get_session, hash_token(token)) is None


def test_me_and_logout(client):
    token = "me-endpoint-token"
    client.portal.call(db.create_session, hash_token(token), ART_DID, ART_HANDLE, 3600)  # type: ignore[attr-defined]

    r = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    assert r.json() == {"did": ART_DID, "handle": ART_HANDLE}

    auth = {"Authorization": f"Bearer {token}"}
    assert client.post("/api/auth/logout", headers=auth).status_code == 200
    assert client.get("/api/auth/me", headers=auth).status_code == 401


def test_session_token_authenticates_the_api(client):
    """A session minted by OAuth is what the rest of the API accepts."""
    token = "api-session-token"
    client.portal.call(db.create_session, hash_token(token), ART_DID, ART_HANDLE, 3600)  # type: ignore[attr-defined]

    r = client.post("/api/canvases", headers={"Authorization": f"Bearer {token}"},
                    json={"title": "made with an oauth session"})
    assert r.status_code == 200, r.text
    assert r.json()["owner_did"] == ART_DID, "canvas must be owned by the OAuth identity"


# --- the callback's security checks -----------------------------------------

def _stash_auth_request(client, state: str, did: str = ART_DID):
    client.portal.call(  # type: ignore[attr-defined]
        db.save_auth_request,
        {
            "state": state,
            "authserver_iss": "https://pds.theblueai.org",
            "did": did,
            "handle": ART_HANDLE,
            "pds_url": "https://pds.theblueai.org",
            "pkce_verifier": "verifier",
            "dpop_private_jwk": oa.new_dpop_key().as_json(is_private=True),
            "dpop_authserver_nonce": "",
        },
    )


def test_callback_rejects_unknown_state(client):
    r = client.get("/oauth/callback?state=never-seen&code=x", follow_redirects=False)
    assert r.status_code == 302
    assert "error=" in r.headers["location"]


def test_callback_rejects_missing_params(client):
    for qs in ["", "?state=abc", "?code=abc"]:
        r = client.get(f"/oauth/callback{qs}", follow_redirects=False)
        assert r.status_code == 302
        assert "error=" in r.headers["location"]


def test_callback_rejects_issuer_mismatch(client):
    _stash_auth_request(client, "state-iss-mismatch")
    r = client.get(
        "/oauth/callback?state=state-iss-mismatch&code=x&iss=https://evil.example.com",
        follow_redirects=False,
    )
    assert r.status_code == 302
    assert "error=" in r.headers["location"]


def test_auth_request_is_single_use(client, monkeypatch):
    """Replaying a code must fail even if the first attempt errored."""
    _stash_auth_request(client, "state-replay")
    first = client.get("/oauth/callback?state=state-replay&code=x", follow_redirects=False)
    second = client.get("/oauth/callback?state=state-replay&code=x", follow_redirects=False)
    assert first.status_code == 302 and second.status_code == 302
    assert "error=" in second.headers["location"], "state must be consumed on first use"


def test_callback_rejects_sub_mismatch(client, monkeypatch):
    """THE load-bearing check.

    If the token endpoint returns a `sub` for a different account than the one
    the flow was started for, we must refuse. Without this, anyone able to
    complete an authorization could be logged in as anybody.
    """
    _stash_auth_request(client, "state-sub-mismatch", did=ART_DID)

    async def fake_token_request(**kwargs):
        return {"sub": "did:plc:someoneelse00000000000000", "access_token": "x"}, ""

    monkeypatch.setattr("app.oauth_routes.oa.initial_token_request", fake_token_request)

    r = client.get("/oauth/callback?state=state-sub-mismatch&code=x", follow_redirects=False)
    assert r.status_code == 302
    loc = r.headers["location"]
    assert "error=" in loc
    assert "session=" not in loc, "a mismatched subject must never mint a session"


def test_callback_mints_session_on_matching_sub(client, monkeypatch):
    _stash_auth_request(client, "state-happy", did=ART_DID)

    async def fake_token_request(**kwargs):
        return {"sub": ART_DID, "access_token": "x"}, ""

    monkeypatch.setattr("app.oauth_routes.oa.initial_token_request", fake_token_request)

    r = client.get("/oauth/callback?state=state-happy&code=x", follow_redirects=False)
    assert r.status_code == 302
    loc = r.headers["location"]
    assert "#session=" in loc, f"expected a session in the redirect, got {loc}"

    token = loc.split("#session=")[1]
    me = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    assert me.json()["did"] == ART_DID


# --- live PDS (opt-in) ------------------------------------------------------

@pytest.mark.network
@pytest.mark.asyncio
async def test_resolve_real_identity():
    did, handle, doc = await oa.resolve_identity(ART_HANDLE)
    assert did == ART_DID
    assert handle == ART_HANDLE
    assert oa.pds_endpoint(doc) == "https://pds.theblueai.org"


@pytest.mark.network
@pytest.mark.asyncio
async def test_real_authserver_metadata_is_valid():
    authserver = await oa.resolve_pds_authserver("https://pds.theblueai.org")
    meta = await oa.fetch_authserver_meta(authserver)
    assert meta["issuer"] == "https://pds.theblueai.org"
    assert meta["pushed_authorization_request_endpoint"]
