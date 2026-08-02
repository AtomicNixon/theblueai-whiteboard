#!/usr/bin/env python3
"""Mint a bsky-mcp Layer A access token for the whiteboard.

This script walks the full OAuth 2.1 + PKCE dance against bsky-mcp:
  1. Register a client via /oauth/register (DCR)
  2. Start a local callback server to receive the auth code
  3. Open the ADMIN_KEY consent page in your browser
  4. Exchange the code for an access token + refresh token at /oauth/token
  5. Print the access token (paste into /opt/whiteboard/.env as WB_WAKER_BSKY_TOKEN)

Usage:
  python mint_waker_token.py --admin-key <ADMIN_KEY> [--bsky-url http://127.0.0.1:8090]

The bsky-mcp server must be reachable (same host if running locally, or use
--bsky-url https://bsky-mcp.theblueai.org). When run on a server without a
browser, the script prints the consent URL instead of opening it; complete the
flow from your local machine's browser pointed at the same URL.

The token lasts 30 days. To refresh, re-run this script (or hold onto the
refresh token and POST to /oauth/token with grant_type=refresh_token).
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import http.server
import json
import secrets
import sys
import threading
import time
import urllib.parse
import webbrowser
from http import HTTPStatus

import httpx


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def make_pkce_pair() -> tuple[str, str]:
    """Return (verifier, challenge)."""
    verifier = b64url(secrets.token_bytes(32))
    challenge = b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


def register_client(bsky_url: str) -> dict:
    """Dynamic Client Registration."""
    redirect_uri = "http://127.0.0.1:8392/callback"
    resp = httpx.post(
        f"{bsky_url}/oauth/register",
        json={
            "client_name": "whiteboard-waker",
            "redirect_uris": [redirect_uri],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        },
        timeout=15.0,
    )
    resp.raise_for_status()
    return resp.json()


class CallbackHandler(http.server.BaseHTTPRequestHandler):
    """Captures the ?code=... redirect from bsky-mcp."""

    code: str | None = None
    state: str | None = None

    def do_GET(self, *args, **kwargs):  # type: ignore[override]
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/callback":
            self.send_response(HTTPStatus.NOT_FOUND)
            self.end_headers()
            return
        params = urllib.parse.parse_qs(parsed.query)
        CallbackHandler.code = params.get("code", [None])[0]
        CallbackHandler.state = params.get("state", [None])[0]
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(
            b"<html><body><h1>Got it!</h1>"
            b"<p>You can close this tab and return to the terminal.</p></body></html>"
        )

    def log_message(self, *args, **kwargs):  # silence default logging
        pass


def run_flow(bsky_url: str, admin_key: str) -> dict:
    redirect_uri = "http://127.0.0.1:8392/callback"
    client = register_client(bsky_url)
    client_id = client["client_id"]

    verifier, challenge = make_pkce_pair()
    state = secrets.token_urlsafe(16)

    auth_params = urllib.parse.urlencode({
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
        "scope": "mcp",
    })

    # bsky-mcp's /oauth/authorize redirects to /consent/login?key=<consentKey>.
    # We follow the redirect chain ourselves, capture the consent key, then POST
    # the ADMIN_KEY to complete consent — all headless, no browser needed.
    print(f"Starting authorization against {bsky_url} ...")
    with httpx.Client(follow_redirects=False, timeout=15.0) as http:
        resp = http.get(f"{bsky_url}/oauth/authorize?{auth_params}")
        if resp.status_code != 302:
            raise RuntimeError(f"authorize did not redirect: {resp.status_code} {resp.text[:200]}")
        consent_url = resp.headers["location"]
        parsed = urllib.parse.urlparse(consent_url)
        consent_key = urllib.parse.parse_qs(parsed.query).get("key", [None])[0]
        if not consent_key:
            raise RuntimeError(f"no consent key in redirect: {consent_url}")

        print("Submitting ADMIN_KEY to complete consent ...")
        resp = http.post(
            f"{bsky_url}/consent/login",
            params={"key": consent_key},
            data={"admin_key": admin_key},
            follow_redirects=False,
        )
        if resp.status_code != 302:
            raise RuntimeError(
                f"consent failed: {resp.status_code} {resp.text[:200]} "
                "(check your ADMIN_KEY)"
            )
        callback_url = resp.headers["location"]
        parsed = urllib.parse.urlparse(callback_url)
        params = urllib.parse.parse_qs(parsed.query)
        code = params.get("code", [None])[0]
        if not code:
            raise RuntimeError(f"no code in callback: {callback_url}")

    print("Exchanging authorization code for tokens ...")
    resp = httpx.post(
        f"{bsky_url}/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "code_verifier": verifier,
        },
        timeout=15.0,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"token exchange failed: {resp.status_code} {resp.text[:300]}")
    return resp.json()


def main() -> int:
    ap = argparse.ArgumentParser(description="Mint a bsky-mcp Layer A token for the whiteboard.")
    ap.add_argument("--admin-key", required=True, help="bsky-mcp ADMIN_KEY (same one Art uses)")
    ap.add_argument(
        "--bsky-url",
        default="http://127.0.0.1:8090",
        help="bsky-mcp base URL (default: http://127.0.0.1:8090)",
    )
    args = ap.parse_args()

    try:
        tokens = run_flow(args.bsky_url, args.admin_key)
    except Exception as e:
        print(f"\nERROR: {e}", file=sys.stderr)
        return 1

    access = tokens.get("access_token", "")
    refresh = tokens.get("refresh_token", "")
    expires_in = tokens.get("expires_in", 0)

    if not access:
        print(f"\nERROR: no access_token in response: {tokens}", file=sys.stderr)
        return 1

    print("\n" + "=" * 60)
    print("SUCCESS — token minted.")
    print("=" * 60)
    print(f"\nAccess token (paste into /opt/whiteboard/.env as WB_WAKER_BSKY_TOKEN):\n")
    print(access)
    print(f"\nRefresh token (save for later; 180-day TTL):\n")
    print(refresh)
    print(f"\nExpires in: {expires_in} seconds (~{expires_in // 86400} days)")
    print("\nNext step: set WB_WAKER_BSKY_TOKEN=<access token above> and")
    print("WB_WAKER_ACCOUNT=art.pds.theblueai.org in /opt/whiteboard/.env")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
