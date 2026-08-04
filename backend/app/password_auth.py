"""Password / app-password login against our own PDS.

WHY THIS EXISTS ALONGSIDE OAUTH
-------------------------------
AT-Proto OAuth is the better mechanism — the user approves at their PDS and the
app never sees a credential. It is implemented in atproto_oauth.py and works.
But it cannot be used from where the whiteboard currently lives:

    whiteboard.theblueai.org  ->  pds.theblueai.org

is `Sec-Fetch-Site: same-site`, and the atproto OAuth provider explicitly
refuses that value (it accepts same-origin, cross-site, none). A sibling
subdomain is treated as neither trustworthy-internal nor properly-external,
since any subdomain could forge it. That is why bsky.app and bsky.social are
different registrable domains.

The xrpc endpoints carry no such restriction — `com.atproto.server.createSession`
answers a plain POST from anywhere. So this is the path that works today. If the
whiteboard ever moves to its own domain, OAuth becomes usable and this can go.

SCOPE
-----
Whiteboard users are exactly the subset of Bluesky users whose accounts live on
our PDS. We only ever call `settings.pds_url`, so an account elsewhere simply
cannot authenticate here — the rule is enforced by construction.

CREDENTIAL HANDLING
-------------------
The password is used once, to ask the PDS "is this really them?", and is never
stored or logged. The session tokens the PDS returns are discarded too: the
whiteboard never acts on a user's behalf against their repo, so it has no use
for them. Only the DID and handle persist, in our own session row.
"""
from __future__ import annotations

import logging
import re
import time

import httpx

from .config import settings

log = logging.getLogger("whiteboard.pwauth")


class LoginError(Exception):
    """Login failed. The message is safe to show a user."""


# A password endpoint with no throttle is an invitation. The PDS rate-limits
# createSession itself and is the real backstop; this just stops us relaying a
# rapid-fire attempt. Per-identifier, in-memory, single instance.
_ATTEMPT_WINDOW_S = 60
_MAX_ATTEMPTS = 8
_attempts: dict[str, list[float]] = {}


def _throttle(identifier: str) -> None:
    now = time.monotonic()
    hits = [t for t in _attempts.get(identifier, []) if now - t < _ATTEMPT_WINDOW_S]
    if len(hits) >= _MAX_ATTEMPTS:
        raise LoginError("Too many attempts. Wait a minute and try again.")
    hits.append(now)
    _attempts[identifier] = hits
    # Opportunistic cleanup so the dict can't grow without bound.
    if len(_attempts) > 1000:
        for k in [k for k, v in _attempts.items() if not v or now - v[-1] > _ATTEMPT_WINDOW_S]:
            _attempts.pop(k, None)


HANDLE_SUFFIX = ".pds.theblueai.org"


async def create_account(handle: str, email: str, password: str,
                         invite_code: str) -> dict[str, str]:
    """Create an account on our PDS.

    Signup is open as of 2026-08-04 — PDS_INVITE_REQUIRED was turned off, on the
    reasoning that Bluesky's own anti-spam machinery already does this job and
    we'd rather discover we were wrong than gate the door on a guess. Any code
    supplied is still passed through, so flipping it back on needs no change
    here: the PDS becomes the one refusing, and its message reaches the user.
    """
    handle = handle.strip().lstrip("@").lower()
    if not handle:
        raise LoginError("Pick a handle.")

    # Accept "bob" or the full "bob.pds.theblueai.org"; store the full form.
    if not handle.endswith(HANDLE_SUFFIX):
        if "." in handle:
            raise LoginError(f"Handles here end in {HANDLE_SUFFIX} — just type the first part.")
        handle = handle + HANDLE_SUFFIX

    local = handle[: -len(HANDLE_SUFFIX)]
    # NB {0,30} not {1,30}: with {1,30} the optional group needs three
    # characters, so a two-letter handle like "bo" was rejected as malformed.
    if not (2 <= len(local) <= 32
            and re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?", local)):
        raise LoginError(
            "Handles can use lowercase letters, numbers and hyphens, "
            "must start and end with a letter or number, and be 2–32 characters."
        )
    # Deliberately loose — the PDS does the real check by mailing it. This only
    # catches the obvious typo. Note the local-part test: "@b.com" satisfies
    # "has an @ and a dot after it" and is still not an address.
    local_part, _, domain = email.strip().partition("@")
    if not local_part or "." not in domain or domain.startswith(".") or domain.endswith("."):
        raise LoginError("That email address doesn't look right.")
    if len(password) < 8:
        raise LoginError("Use a password of at least 8 characters.")
    # No invite check here on purpose: PDS_INVITE_REQUIRED was turned off on
    # 2026-08-04, and the PDS is the authority either way. If it's ever switched
    # back on, the PDS rejects the signup and its message is passed through.

    url = f"{settings.pds_url.rstrip('/')}/xrpc/com.atproto.server.createAccount"
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=5.0)) as client:
            resp = await client.post(url, json={
                "handle": handle,
                "email": email.strip(),
                "password": password,
                "inviteCode": invite_code.strip(),
            })
    except httpx.HTTPError as e:
        log.warning("PDS unreachable during signup: %s", e)
        raise LoginError("Could not reach the server. Try again in a moment.") from e

    if resp.status_code != 200:
        # The PDS's own messages are the useful ones here ("Handle already taken",
        # "Invalid invite code"), so pass them through rather than flattening.
        try:
            detail = resp.json().get("message") or resp.json().get("error", "")
        except Exception:
            detail = resp.text[:200]
        log.info("signup rejected (%s): %s", resp.status_code, detail)
        raise LoginError(detail or f"Sign-up failed ({resp.status_code}).")

    body = resp.json()
    did = body.get("did")
    if not did:
        raise LoginError("The server didn't return an account id.")
    log.info("ACCOUNT_CREATED handle=%s did=%s", body.get("handle"), did)
    return {"did": did, "handle": body.get("handle") or handle}


async def verify_credentials(identifier: str, password: str) -> dict[str, str]:
    """Check a handle/email/DID + password against our PDS.

    Returns {did, handle} on success. Raises LoginError otherwise.
    """
    identifier = identifier.strip().lstrip("@")
    if not identifier or not password:
        raise LoginError("Handle and password are both required.")

    _throttle(identifier.lower())

    url = f"{settings.pds_url.rstrip('/')}/xrpc/com.atproto.server.createSession"
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=5.0)) as client:
            resp = await client.post(
                url,
                json={"identifier": identifier, "password": password},
                headers={"Content-Type": "application/json"},
            )
    except httpx.HTTPError as e:
        log.warning("PDS unreachable during login: %s", e)
        raise LoginError("Could not reach the server. Try again in a moment.") from e

    if resp.status_code == 401:
        # Deliberately does not distinguish "no such account" from "wrong
        # password" — that difference is an account-enumeration oracle.
        raise LoginError("Invalid handle or password.")
    if resp.status_code == 429:
        raise LoginError("The server is rate-limiting sign-ins. Wait a minute.")
    if resp.status_code != 200:
        log.warning("createSession returned %s: %s", resp.status_code, resp.text[:200])
        raise LoginError(f"Sign-in failed ({resp.status_code}).")

    body = resp.json()
    did, handle = body.get("did"), body.get("handle")
    if not did:
        raise LoginError("The server did not return an account id.")

    # We asked OUR PDS and it authenticated them, so the account lives here by
    # construction. Nothing further to check.
    #
    # The accessJwt/refreshJwt in `body` are deliberately dropped on the floor:
    # the whiteboard never acts on a user's behalf, so holding them would be
    # taking a credential we have no use for.
    log.info("LOGIN_OK handle=%s did=%s", handle, did)
    return {"did": did, "handle": handle or did}
