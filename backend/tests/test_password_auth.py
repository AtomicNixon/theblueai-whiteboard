"""Password / app-password login against our own PDS.

The PDS's `createSession` is stubbed so the whole path can be exercised without
anyone's real credential: throttling, error mapping, session minting, and that
the resulting session actually authenticates the rest of the API.

What's being protected here:
  - the password is used once and never stored or returned
  - the PDS's session tokens are discarded, not kept
  - a failed login can't tell you whether the account exists
  - repeated attempts get throttled before they reach the PDS
"""
from __future__ import annotations

import os
import sys

import httpx
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import password_auth as pw  # noqa: E402

GOOD = {
    "did": "did:plc:f5nmd2dfjm4johmbbeyoyve3",
    "handle": "art.pds.theblueai.org",
    "accessJwt": "SECRET-ACCESS-JWT-SHOULD-NOT-BE-KEPT",
    "refreshJwt": "SECRET-REFRESH-JWT-SHOULD-NOT-BE-KEPT",
}


@pytest.fixture(autouse=True)
def clear_throttle():
    pw._attempts.clear()
    yield
    pw._attempts.clear()


def stub_pds(monkeypatch, *, status=200, body=None, raises=None, capture=None):
    """Replace the outbound createSession call with a canned response."""
    async def fake_post(self, url, **kwargs):
        if capture is not None:
            capture["url"] = url
            capture["json"] = kwargs.get("json")
        if raises:
            raise raises
        return httpx.Response(status, json=body if body is not None else {},
                              request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)


# --- success ----------------------------------------------------------------

@pytest.mark.asyncio
async def test_valid_credentials_return_identity(monkeypatch):
    cap: dict = {}
    stub_pds(monkeypatch, body=GOOD, capture=cap)

    who = await pw.verify_credentials("art.pds.theblueai.org", "hunter2")

    assert who == {"did": GOOD["did"], "handle": GOOD["handle"]}
    # Only the identity survives — the PDS's tokens are dropped on the floor.
    assert "accessJwt" not in who and "refreshJwt" not in who


@pytest.mark.asyncio
async def test_calls_our_own_pds_only(monkeypatch):
    """Whiteboard users are accounts on OUR server. The rule is enforced by
    only ever asking our PDS — not by a check we could forget."""
    cap: dict = {}
    stub_pds(monkeypatch, body=GOOD, capture=cap)
    await pw.verify_credentials("art.pds.theblueai.org", "pw")

    assert cap["url"].startswith("https://pds.theblueai.org/")
    assert cap["url"].endswith("/xrpc/com.atproto.server.createSession")


@pytest.mark.asyncio
async def test_leading_at_and_whitespace_tolerated(monkeypatch):
    cap: dict = {}
    stub_pds(monkeypatch, body=GOOD, capture=cap)
    await pw.verify_credentials("  @art.pds.theblueai.org ", "pw")
    assert cap["json"]["identifier"] == "art.pds.theblueai.org"


# --- failure modes ----------------------------------------------------------

@pytest.mark.asyncio
async def test_bad_password_is_generic(monkeypatch):
    """Must not distinguish 'no such account' from 'wrong password' — that
    difference is an account-enumeration oracle."""
    stub_pds(monkeypatch, status=401,
             body={"error": "AuthenticationRequired", "message": "Invalid identifier or password"})
    with pytest.raises(pw.LoginError) as e:
        await pw.verify_credentials("art.pds.theblueai.org", "wrong")
    assert "Invalid handle or password" in str(e.value)
    assert "identifier" not in str(e.value).lower()


@pytest.mark.asyncio
async def test_rate_limited_upstream(monkeypatch):
    stub_pds(monkeypatch, status=429, body={"error": "RateLimitExceeded"})
    with pytest.raises(pw.LoginError) as e:
        await pw.verify_credentials("art.pds.theblueai.org", "pw")
    assert "rate-limiting" in str(e.value)


@pytest.mark.asyncio
async def test_pds_unreachable(monkeypatch):
    stub_pds(monkeypatch, raises=httpx.ConnectError("no route"))
    with pytest.raises(pw.LoginError) as e:
        await pw.verify_credentials("art.pds.theblueai.org", "pw")
    assert "Could not reach" in str(e.value)


@pytest.mark.asyncio
async def test_response_without_did_rejected(monkeypatch):
    stub_pds(monkeypatch, body={"handle": "art.pds.theblueai.org"})
    with pytest.raises(pw.LoginError):
        await pw.verify_credentials("art.pds.theblueai.org", "pw")


@pytest.mark.asyncio
@pytest.mark.parametrize("ident,password", [("", "pw"), ("art.pds.theblueai.org", ""), ("", "")])
async def test_missing_fields_rejected(monkeypatch, ident, password):
    stub_pds(monkeypatch, body=GOOD)
    with pytest.raises(pw.LoginError):
        await pw.verify_credentials(ident, password)


# --- throttling -------------------------------------------------------------

@pytest.mark.asyncio
async def test_throttle_kicks_in(monkeypatch):
    stub_pds(monkeypatch, status=401, body={"error": "AuthenticationRequired"})

    for _ in range(pw._MAX_ATTEMPTS):
        with pytest.raises(pw.LoginError) as e:
            await pw.verify_credentials("art.pds.theblueai.org", "wrong")
        assert "Too many attempts" not in str(e.value)

    with pytest.raises(pw.LoginError) as e:
        await pw.verify_credentials("art.pds.theblueai.org", "wrong")
    assert "Too many attempts" in str(e.value)


@pytest.mark.asyncio
async def test_throttle_is_per_identifier(monkeypatch):
    stub_pds(monkeypatch, status=401, body={})
    for _ in range(pw._MAX_ATTEMPTS + 1):
        with pytest.raises(pw.LoginError):
            await pw.verify_credentials("victim.pds.theblueai.org", "x")

    # A different account must not be locked out by someone else's attempts.
    stub_pds(monkeypatch, body=GOOD)
    who = await pw.verify_credentials("art.pds.theblueai.org", "pw")
    assert who["did"] == GOOD["did"]


# --- the HTTP endpoint ------------------------------------------------------

def test_login_endpoint_mints_a_working_session(client, monkeypatch):
    stub_pds(monkeypatch, body=GOOD)

    r = client.post("/api/auth/login-password",
                    json={"identifier": "art.pds.theblueai.org", "password": "pw"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["did"] == GOOD["did"]
    assert body["handle"] == GOOD["handle"]

    # The password must not come back, and neither may the PDS's tokens.
    blob = r.text
    assert "pw" not in body.values()
    assert GOOD["accessJwt"] not in blob
    assert GOOD["refreshJwt"] not in blob

    # And the session it issued must actually authenticate the API, as the
    # right person.
    auth = {"Authorization": f"Bearer {body['session']}"}
    me = client.get("/api/auth/me", headers=auth)
    assert me.status_code == 200
    assert me.json()["did"] == GOOD["did"]

    made = client.post("/api/canvases", headers=auth, json={"title": "art's canvas"})
    assert made.status_code == 200
    assert made.json()["owner_did"] == GOOD["did"]


def test_login_endpoint_401s_on_bad_password(client, monkeypatch):
    stub_pds(monkeypatch, status=401, body={"error": "AuthenticationRequired"})
    r = client.post("/api/auth/login-password",
                    json={"identifier": "art.pds.theblueai.org", "password": "nope"})
    assert r.status_code == 401
    assert "Invalid handle or password" in r.json()["detail"]


def test_two_people_are_two_identities(client, monkeypatch):
    """The whole point. Two logins must produce two distinct owners."""
    stub_pds(monkeypatch, body=GOOD)
    art = client.post("/api/auth/login-password",
                      json={"identifier": "art.pds.theblueai.org", "password": "pw"}).json()

    bob = dict(GOOD, did="did:plc:oxpkcsfmui5q5tdccah2svij", handle="bob.pds.theblueai.org")
    stub_pds(monkeypatch, body=bob)
    bob_session = client.post("/api/auth/login-password",
                              json={"identifier": "bob.pds.theblueai.org", "password": "pw"}).json()

    assert art["did"] != bob_session["did"]
    assert art["session"] != bob_session["session"]

    # Art's text must not be editable by Bob.
    art_auth = {"Authorization": f"Bearer {art['session']}"}
    bob_auth = {"Authorization": f"Bearer {bob_session['session']}"}

    canvas = client.post("/api/canvases", headers=art_auth, json={"title": "shared"}).json()
    el = client.post(f"/api/canvases/{canvas['id']}/elements", headers=art_auth,
                     json={"kind": "text", "data": {"text": "art's note", "x": 1, "y": 2}}).json()

    hijack = client.patch(f"/api/elements/{el['id']}", headers=bob_auth,
                          json={"data": {"text": "bob was here", "x": 1, "y": 2}})
    assert hijack.status_code == 403, "text must be single-owner across real identities"
