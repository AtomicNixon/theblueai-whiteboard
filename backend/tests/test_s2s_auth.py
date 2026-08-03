"""Server-to-server auth for AI agents.

Browser clients hold a bsky-mcp OAuth access token. AI agents arriving through
bsky-mcp's wb_* MCP tools do not — bsky-mcp knows who the caller is but holds no
bearer credential to forward — so it presents a shared secret plus the actor's
DID.

This is an authentication bypass path by construction, so it is tested
adversarially: the failure that matters is not "does it work" but "can it be
made to authenticate someone it shouldn't".

These tests exercise validate_s2s directly and need no database.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.auth import AuthError, validate_s2s  # noqa: E402
from app.config import settings  # noqa: E402

SECRET = "s3cret-shared-with-bsky-mcp"
ACTOR = "did:plc:bob00000000000000000000000"


@pytest.fixture
def s2s_enabled():
    original = settings.s2s_secret
    settings.s2s_secret = SECRET
    yield
    settings.s2s_secret = original


@pytest.fixture
def s2s_disabled():
    original = settings.s2s_secret
    settings.s2s_secret = ""
    yield
    settings.s2s_secret = original


# --- the happy path --------------------------------------------------------

def test_valid_s2s_call_authenticates_as_the_actor(s2s_enabled):
    who = validate_s2s(SECRET, ACTOR)
    assert who["did"] == ACTOR


# --- the ones that matter --------------------------------------------------

def test_disabled_when_secret_unconfigured(s2s_disabled):
    """The dangerous case: empty configured secret must not match empty input.

    Without the explicit guard, `"" == ""` would authenticate an attacker who
    sent two empty headers as any DID they named.
    """
    with pytest.raises(AuthError):
        validate_s2s("", ACTOR)
    with pytest.raises(AuthError):
        validate_s2s(None, ACTOR)
    with pytest.raises(AuthError):
        validate_s2s(SECRET, ACTOR)


def test_wrong_secret_rejected(s2s_enabled):
    for bad in ["", None, "wrong", SECRET + "x", SECRET[:-1], SECRET.upper(), " " + SECRET]:
        with pytest.raises(AuthError):
            validate_s2s(bad, ACTOR)


def test_actor_did_required_and_shaped(s2s_enabled):
    """A correct secret alone must not authenticate — it names no one."""
    for bad in [None, "", "bob.pds.theblueai.org", "not-a-did", "  ", "didplc:x"]:
        with pytest.raises(AuthError):
            validate_s2s(SECRET, bad)


def test_secret_alone_grants_nothing_without_actor(s2s_enabled):
    with pytest.raises(AuthError):
        validate_s2s(SECRET, None)


# --- HTTP surface ----------------------------------------------------------

def test_s2s_headers_reach_the_api(client, canvas, s2s_enabled):
    """An AI agent posting a text element exactly as bsky-mcp will."""
    from app.auth import S2S_ACTOR_HEADER, S2S_SECRET_HEADER

    headers = {S2S_SECRET_HEADER: SECRET, S2S_ACTOR_HEADER: ACTOR}
    r = client.post(
        f"/api/canvases/{canvas['id']}/elements",
        headers=headers,
        json={"kind": "text", "data": {"text": "drawn by an agent", "x": 50, "y": 60}},
    )
    assert r.status_code == 200, r.text
    assert r.json()["owner_did"] == ACTOR, "the element must be attributed to the actor"

    # And it comes back renderable, not as the bare partial that was posted.
    snap = client.get(f"/api/canvases/{canvas['id']}/snapshot", headers=headers)
    data = snap.json()["elements"][0]["data"]
    assert data["type"] == "text"
    assert data["text"] == "drawn by an agent"
    assert "fontSize" in data and "seed" in data


def test_bad_s2s_secret_is_401_over_http(client, canvas, s2s_enabled):
    from app.auth import S2S_ACTOR_HEADER, S2S_SECRET_HEADER

    r = client.get(
        f"/api/canvases/{canvas['id']}/snapshot",
        headers={S2S_SECRET_HEADER: "nope", S2S_ACTOR_HEADER: ACTOR},
    )
    assert r.status_code == 401


def test_s2s_header_does_not_shadow_a_bearer_token(client, canvas, alice_headers,
                                                   s2s_enabled):
    """A browser request with no S2S header must still use its bearer token."""
    r = client.get(f"/api/canvases/{canvas['id']}/snapshot", headers=alice_headers)
    assert r.status_code == 200
    assert r.json()["me"].endswith("alice0000000000000000000")
