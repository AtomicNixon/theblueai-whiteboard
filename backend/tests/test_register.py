"""Account creation against our PDS.

Signup is validated locally before the PDS is contacted, so a doofus gets a
useful sentence instead of a protocol error. The tests below are mostly about
that: each one is a mistake a real person makes on a real signup form.
"""
from __future__ import annotations

import os
import sys

import httpx
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import password_auth as pw  # noqa: E402

CREATED = {
    "did": "did:plc:newaccount00000000000000",
    "handle": "newbie.pds.theblueai.org",
    "accessJwt": "SECRET", "refreshJwt": "SECRET",
}


@pytest.fixture(autouse=True)
def clear_throttle():
    pw._attempts.clear()
    yield
    pw._attempts.clear()


def stub_pds(monkeypatch, *, status=200, body=None, capture=None):
    async def fake_post(self, url, **kwargs):
        if capture is not None:
            capture["url"] = url
            capture["json"] = kwargs.get("json")
        return httpx.Response(status, json=body if body is not None else CREATED,
                              request=httpx.Request("POST", url))
    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)


# --- local validation, before the PDS is bothered ---------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("handle,ok", [
    ("bo", True),          # two characters — regressed once; {1,30} needs three
    ("b", False),          # one is too short
    ("bob", True),
    ("bob-the-builder", True),
    ("a" * 32, True),
    ("a" * 33, False),
    ("-bob", False),       # must start alphanumeric
    ("bob-", False),       # and end alphanumeric
    ("Bob", True),         # uppercase is lowercased, not rejected
    ("bob bob", False),
    ("bob_bob", False),
    ("bob.pds.theblueai.org", True),   # full form accepted
    ("bob.example.com", False),        # someone else's domain
])
async def test_handle_rules(monkeypatch, handle, ok):
    stub_pds(monkeypatch)
    if ok:
        who = await pw.create_account(handle, "a@b.com", "longenough", "code")
        assert who["did"] == CREATED["did"]
    else:
        with pytest.raises(pw.LoginError):
            await pw.create_account(handle, "a@b.com", "longenough", "code")


@pytest.mark.asyncio
async def test_bare_handle_gets_our_suffix(monkeypatch):
    cap: dict = {}
    stub_pds(monkeypatch, capture=cap)
    await pw.create_account("newbie", "a@b.com", "longenough", "code")
    assert cap["json"]["handle"] == "newbie.pds.theblueai.org"


@pytest.mark.asyncio
async def test_uppercase_and_at_are_tolerated(monkeypatch):
    cap: dict = {}
    stub_pds(monkeypatch, capture=cap)
    await pw.create_account("  @NewBie ", "a@b.com", "longenough", "code")
    assert cap["json"]["handle"] == "newbie.pds.theblueai.org"


@pytest.mark.asyncio
@pytest.mark.parametrize("email", ["nope", "no@domain", "@b.com", ""])
async def test_bad_email_rejected(monkeypatch, email):
    stub_pds(monkeypatch)
    with pytest.raises(pw.LoginError, match="email"):
        await pw.create_account("newbie", email, "longenough", "code")


@pytest.mark.asyncio
async def test_short_password_rejected(monkeypatch):
    stub_pds(monkeypatch)
    with pytest.raises(pw.LoginError, match="8 characters"):
        await pw.create_account("newbie", "a@b.com", "short", "code")


@pytest.mark.asyncio
async def test_no_invite_code_needed(monkeypatch):
    """Signup opened 2026-08-04. An empty code must not be refused here."""
    cap: dict = {}
    stub_pds(monkeypatch, capture=cap)
    who = await pw.create_account("newbie", "a@b.com", "longenough", "")
    assert who["did"] == CREATED["did"]
    assert cap["json"]["inviteCode"] == ""


@pytest.mark.asyncio
async def test_invite_code_still_forwarded(monkeypatch):
    """The PDS stays the authority. If invites are switched back on, a code
    still reaches it and its refusal still reaches the user."""
    cap: dict = {}
    stub_pds(monkeypatch, capture=cap)
    await pw.create_account("newbie", "a@b.com", "longenough", "  some-code  ")
    assert cap["json"]["inviteCode"] == "some-code"


@pytest.mark.asyncio
async def test_pds_can_still_refuse_for_a_bad_invite(monkeypatch):
    stub_pds(monkeypatch, status=400,
             body={"error": "InvalidInviteCode", "message": "Provided invite code not available"})
    with pytest.raises(pw.LoginError, match="invite code not available"):
        await pw.create_account("newbie", "a@b.com", "longenough", "nope")


# --- talking to the PDS -----------------------------------------------------

@pytest.mark.asyncio
async def test_creates_on_our_own_pds(monkeypatch):
    cap: dict = {}
    stub_pds(monkeypatch, capture=cap)
    await pw.create_account("newbie", "a@b.com", "longenough", "code")
    assert cap["url"].startswith("https://pds.theblueai.org/")
    assert cap["url"].endswith("com.atproto.server.createAccount")
    assert cap["json"]["inviteCode"] == "code"


@pytest.mark.asyncio
async def test_pds_message_is_passed_through(monkeypatch):
    """"Handle already taken" is more use than "sign-up failed (400)"."""
    stub_pds(monkeypatch, status=400,
             body={"error": "InvalidHandle", "message": "Handle already taken"})
    with pytest.raises(pw.LoginError, match="Handle already taken"):
        await pw.create_account("newbie", "a@b.com", "longenough", "code")


@pytest.mark.asyncio
async def test_tokens_are_not_returned(monkeypatch):
    stub_pds(monkeypatch)
    who = await pw.create_account("newbie", "a@b.com", "longenough", "code")
    assert set(who) == {"did", "handle"}


# --- the endpoint -----------------------------------------------------------

def test_register_endpoint_signs_you_in(client, monkeypatch):
    stub_pds(monkeypatch)
    r = client.post("/api/auth/register", json={
        "handle": "newbie", "email": "a@b.com",
        "password": "longenough", "inviteCode": "code",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["did"] == CREATED["did"]

    # A new user shouldn't have to log in again ten seconds after choosing a
    # password they just invented.
    me = client.get("/api/auth/me", headers={"Authorization": f"Bearer {body['session']}"})
    assert me.status_code == 200
    assert me.json()["did"] == CREATED["did"]


def test_register_endpoint_reports_problems_as_400(client, monkeypatch):
    stub_pds(monkeypatch)
    r = client.post("/api/auth/register", json={
        "handle": "b", "email": "a@b.com", "password": "longenough", "inviteCode": "code",
    })
    assert r.status_code == 400
    assert "2–32" in r.json()["detail"] or "characters" in r.json()["detail"]
