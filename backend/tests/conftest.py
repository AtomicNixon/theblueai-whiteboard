"""Integration test fixtures — real Postgres in Docker, stubbed bsky-mcp auth.

Why a real database rather than a mock: the whole point of the 2026-08-02
verbatim-storage change is that an Excalidraw element survives the round trip
through JSONB unaltered. A mocked store would happily hand back the Python dict
it was given and prove nothing. The bug being guarded against lives precisely in
serialization, so the serialization has to be real.

Auth is stubbed because it is the one dependency that genuinely can't run
locally — `validate_token` calls out to bsky-mcp's MCP endpoint, which needs the
live VPS. Everything downstream of identity is exercised for real.

Skips cleanly (rather than failing) if Docker isn't available, so the unit tests
in test_elements.py still run anywhere.
"""
from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
import time
import uuid

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PG_IMAGE = "postgres:16-alpine"
PG_PASSWORD = "wbtestpw"
CONTAINER = "wb-pytest-pg"

# Two distinct identities, so ownership rules can actually be tested.
ALICE = {"did": "did:plc:alice0000000000000000000", "handle": "alice.test"}
BOB = {"did": "did:plc:bob00000000000000000000000", "handle": "bob.test"}
TOKENS = {"tok-alice": ALICE, "tok-bob": BOB}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _docker_available() -> bool:
    try:
        subprocess.run(["docker", "version"], capture_output=True, timeout=20, check=True)
        return True
    except Exception:
        return False


@pytest.fixture(scope="session")
def pg_port() -> int:
    """A Postgres to test against.

    If WB_TEST_PG_PORT is set we use that database and manage nothing — this is
    how CI runs, against a `services:` container that's already healthy. Locally
    we start a throwaway container and tear it down at session end.
    """
    external = os.environ.get("WB_TEST_PG_PORT")
    if external:
        yield int(external)
        return

    if not _docker_available():
        pytest.skip("docker unavailable and WB_TEST_PG_PORT unset — skipping integration tests")

    port = _free_port()
    subprocess.run(["docker", "rm", "-f", CONTAINER], capture_output=True)
    subprocess.run(
        ["docker", "run", "-d", "--name", CONTAINER,
         "-e", f"POSTGRES_PASSWORD={PG_PASSWORD}",
         "-e", "POSTGRES_USER=whiteboard",
         "-e", "POSTGRES_DB=whiteboard",
         "-p", f"{port}:5432", PG_IMAGE],
        capture_output=True, check=True,
    )
    try:
        for _ in range(60):
            r = subprocess.run(
                ["docker", "exec", CONTAINER, "pg_isready", "-U", "whiteboard", "-d", "whiteboard"],
                capture_output=True,
            )
            if r.returncode == 0:
                break
            time.sleep(1)
        else:
            raise RuntimeError("postgres did not become ready in 60s")
        # pg_isready can pass a moment before the socket accepts real auth.
        time.sleep(2)
        yield port
    finally:
        subprocess.run(["docker", "rm", "-f", CONTAINER], capture_output=True)


@pytest.fixture(scope="session")
def client(pg_port):
    """A TestClient with a live DB pool and auth stubbed to a token->identity map.

    Two patch points, because the two transports resolve identity differently:

      - HTTP goes through routes.current_user -> auth.authenticate, which looks
        up `validate_token` in auth's module globals at call time. Patching
        app.auth.validate_token covers it.
      - ws_routes.py did `from .auth import validate_token`, so it holds its own
        reference and must be patched directly.

    S2S auth is exercised separately in test_s2s_auth.py with a real secret.
    """
    from starlette.testclient import TestClient

    from app import auth as auth_mod
    from app import db, ws_routes
    from app.auth import AuthError
    from app.config import settings

    settings.pg_host = os.environ.get("WB_TEST_PG_HOST", "127.0.0.1")
    settings.pg_port = pg_port
    settings.pg_db = "whiteboard"
    settings.pg_user = "whiteboard"
    settings.pg_password = os.environ.get("WB_TEST_PG_PASSWORD", PG_PASSWORD)

    async def fake_validate(token: str) -> dict[str, str]:
        who = TOKENS.get(token)
        if who is None:
            raise AuthError("invalid token")
        return who

    auth_mod.validate_token = fake_validate
    ws_routes.validate_token = fake_validate

    # main.py mounts a SPA catch-all only when the static dir exists; point it
    # somewhere absent so unmatched routes 404 honestly during tests.
    os.environ["WB_STATIC_DIR"] = "/nonexistent-static-dir-for-tests"

    from app.main import app

    asyncio.get_event_loop_policy().new_event_loop()
    with TestClient(app) as c:
        yield c

    async def _close():
        if db._pool is not None:
            await db._pool.close()
            db._pool = None

    try:
        asyncio.run(_close())
    except Exception:
        pass


@pytest.fixture
def alice_headers() -> dict[str, str]:
    return {"Authorization": "Bearer tok-alice"}


@pytest.fixture
def bob_headers() -> dict[str, str]:
    return {"Authorization": "Bearer tok-bob"}


@pytest.fixture
def canvas(client, alice_headers) -> dict:
    """A fresh canvas owned by Alice, unique per test."""
    r = client.post("/api/canvases", headers=alice_headers,
                    json={"title": f"test-{uuid.uuid4().hex[:8]}"})
    assert r.status_code == 200, r.text
    return r.json()
