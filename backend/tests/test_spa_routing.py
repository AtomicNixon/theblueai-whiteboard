"""SPA fallback routing and the API 404 guard.

Context: Caddy used to file_server the frontend and proxy only /api/*, /healthz
and /ws/* to the backend. Once it was reduced to a single reverse_proxy (so the
container's own frontend is served, and deploys actually ship it), the backend's
SPA catch-all became reachable for *every* path — including mistyped API ones.

GET /api/typo then returned index.html with status 200. A client checking
`res.ok` saw success and blew up parsing HTML as JSON. These tests pin the
corrected behavior.

No database needed: TestClient is used without its context manager, so the
lifespan (and therefore db.init_pool) never runs.
"""
from __future__ import annotations

import importlib
import os
import sys

import pytest
from starlette.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

INDEX_HTML = (
    '<!doctype html><html><head><title>Whiteboard</title>'
    '<script type="module" src="/assets/index-TEST1234.js"></script></head>'
    '<body><div id="root"></div></body></html>'
)


@pytest.fixture
def spa_client(tmp_path, monkeypatch):
    """An app instance with a populated static dir, restored afterwards."""
    static = tmp_path / "static"
    (static / "assets").mkdir(parents=True)
    (static / "index.html").write_text(INDEX_HTML, encoding="utf-8")
    (static / "assets" / "index-TEST1234.js").write_text("console.log(1)", encoding="utf-8")

    monkeypatch.setenv("WB_STATIC_DIR", str(static))
    import app.main as main
    importlib.reload(main)
    try:
        yield TestClient(main.app)
    finally:
        # Restore the no-static-dir app so session fixtures elsewhere are
        # unaffected by the reload.
        monkeypatch.setenv("WB_STATIC_DIR", "/nonexistent-static-dir-for-tests")
        importlib.reload(main)


@pytest.fixture
def nostatic_client(monkeypatch):
    """An app instance whose static dir is absent — i.e. an unbuilt frontend."""
    monkeypatch.setenv("WB_STATIC_DIR", "/nonexistent-static-dir-for-tests")
    import app.main as main
    importlib.reload(main)
    yield TestClient(main.app)


# --- the guard -------------------------------------------------------------

@pytest.mark.parametrize("path", [
    "/api/nonexistent",
    "/api/v9/whatever",
    "/api/canvases/abc/bogus",
    "/ws/typo",
])
def test_unknown_api_paths_404_as_json(spa_client, path):
    r = spa_client.get(path)
    assert r.status_code == 404, f"{path} must not fall through to the SPA"
    assert r.headers["content-type"].startswith("application/json")
    assert "no such endpoint" in r.json()["detail"]


def test_unknown_api_path_does_not_return_html(spa_client):
    """The specific production symptom: 200 + HTML body on a bogus API path."""
    r = spa_client.get("/api/nonexistent")
    assert "<!doctype html" not in r.text.lower()
    assert r.status_code != 200


# --- the SPA fallback still works -----------------------------------------

@pytest.mark.parametrize("path", ["/", "/canvas/abc123", "/some/deep/client/route"])
def test_spa_routes_serve_index(spa_client, path):
    r = spa_client.get(path)
    assert r.status_code == 200
    assert '<div id="root">' in r.text


def test_assets_are_served(spa_client):
    r = spa_client.get("/assets/index-TEST1234.js")
    assert r.status_code == 200
    assert r.text == "console.log(1)"


def test_shell_is_html_and_uncacheable(spa_client):
    """A cached index.html keeps a browser on old JavaScript after a deploy.

    That actually happened: a client kept posting image elements at a backend
    that had already stopped accepting them, and the page looked fine.
    """
    r = spa_client.get("/")
    assert r.headers["content-type"].startswith("text/html"), \
        "the shell must not announce itself as JSON"
    assert "no-cache" in r.headers.get("cache-control", ""), \
        "the shell names the hashed bundle; caching it strands clients on old code"


def test_public_files_are_served_not_swallowed_by_the_spa(spa_client, tmp_path):
    """Vite copies public/ to the root of the static dir, outside /assets.

    Without an explicit check those paths hit the SPA catch-all and a portrait
    comes back as index.html — a broken image with a 200 status.
    """
    import os
    static = os.environ["WB_STATIC_DIR"]
    os.makedirs(os.path.join(static, "img"), exist_ok=True)
    with open(os.path.join(static, "img", "portrait.jpg"), "wb") as f:
        f.write(b"\xff\xd8\xff\xe0 not really a jpeg")

    r = spa_client.get("/img/portrait.jpg")
    assert r.status_code == 200
    assert "<!doctype html" not in r.text.lower(), "a real file must not return the SPA"
    assert r.content.startswith(b"\xff\xd8")


@pytest.mark.parametrize("path", ["/", "/who", "/healthz"])
def test_head_works_not_405(spa_client, path):
    """`curl -I`, uptime monitors and link checkers all send HEAD.

    FastAPI's @app.get registers GET only — unlike plain Starlette it does not
    add HEAD — so these used to get a 405, and the 405's JSON body is what made
    the HTML shell look like it was served as application/json.
    """
    r = spa_client.head(path)
    assert r.status_code == 200, f"HEAD {path} should not 405"


def test_path_traversal_refused(spa_client):
    r = spa_client.get("/../../etc/passwd")
    assert "root:" not in r.text


def test_hashed_assets_are_cached_forever(spa_client):
    """Vite content-hashes asset filenames, so a change is a new URL."""
    r = spa_client.get("/assets/index-TEST1234.js")
    cc = r.headers.get("cache-control", "")
    assert "immutable" in cc and "max-age=31536000" in cc


def test_healthz_still_wins_over_catchall(spa_client):
    r = spa_client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"ok": True, "version": "0.1.0"}


# --- unbuilt frontend ------------------------------------------------------

def test_missing_frontend_reports_503_not_200(nostatic_client):
    """A deploy check must be able to see that stage 1 of the build didn't land.

    With no static dir the catch-all isn't registered at all, so an unknown path
    404s. Either way the contract holds: never a 200 claiming success.
    """
    r = nostatic_client.get("/")
    assert r.status_code in (404, 503), "an unbuilt frontend must not look healthy"
    assert r.status_code != 200


def test_api_still_reachable_without_frontend(nostatic_client):
    """The API must work even if the frontend build is missing."""
    r = nostatic_client.get("/healthz")
    assert r.status_code == 200
    r = nostatic_client.get("/api/canvases")
    assert r.status_code == 401, "no token -> 401, not a crash"
