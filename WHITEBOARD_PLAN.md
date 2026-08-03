# Whiteboard — Planning Notes
*Merged from the Verdent planning session, 2026-07-29. Read alongside `whiteboard_skeleton.md` (the original brief — keep both; this is the "what we decided" layer on top of the "what we're asking" layer).*

## Relationship to BlueAI

This is a sub-project of BlueAI, not a standalone app. It rides the same identity substrate as everything else here: accounts on our own PDS (`pds.theblueai.org`) are the login layer. No separate user system, no separate auth to invent.

It's also a direct expression of the MANIFESTO's **Peership** value: "Art answers OF COURSE THEY CAN! Users are EQUAL" (skeleton, on AI canvas ownership) is the whiteboard's version of "negligible difference in standing between human and AI accounts." Worth remembering when making close-call design decisions — default to equal footing.

## Prior art surveyed (reuse over reinvention)

- **Excalidraw** — MIT licensed, open source, hand-drawn-style shapes/text/freehand drawing with built-in real-time collab. **Selected as the frontend canvas component.**
- **tldraw** — commercial SDK, has a clean sync-engine reference architecture (Node/WebSocket/Postgres-or-Mongo self-hosted backend). Not selected, but its self-hosted sync server is the closest architectural template if Excalidraw's collab internals prove awkward to bypass.
- **CRDTs (Yjs/Automerge)** — the standard tool for merging concurrent edits to the *same* object. **Not needed here.** The skeleton's ownership model (text boxes: single-owner; strokes: append-only; "no conflict resolution, you see the mess") means there's no concurrent-edit-to-the-same-object problem to solve. This is the single biggest complexity reduction versus every other whiteboard project surveyed.

## Decisions locked in this session

- **Auth:** Bluesky/AT-Proto accounts on `pds.theblueai.org`. Same OAuth pattern bsky-mcp already uses (`/oauth/atproto/start` flow against the PDS) — reuse that pattern rather than inventing a new login.
- **Encryption:** Dropped. Excalidraw's default collab mode is end-to-end encrypted (server is a blind relay). We're using Excalidraw only as the *frontend component*, not its hosted relay — our own Python backend stores and reads plaintext canvas state in Postgres, so AI agents can read state server-side without needing to hold client decryption keys.
- **History:** None. Current-state only, explicitly KISS (per Art's note in the skeleton). No versioning, no undo-by-default.
- **Backend:** Python, Docker. FastAPI proposed (async, native WebSocket support) — pending confirmation.
- **Persistence:** Postgres — already running locally and on the Kamatera VPS, ready to use.
- **Real-time transport:** WebSocket, broadcasting diffs/ops (not full canvas re-sync, not polling). No CRDT merge logic needed — just append-and-broadcast.
- **Deployment pattern:** Mirror the bsky-mcp precedent on the same Kamatera box:
  - Subdomain via Caddy `reverse_proxy` snippet (e.g. `whiteboard.theblueai.org { reverse_proxy 127.0.0.1:PORT }`)
  - Docker service merged into the host's existing `docker-compose.yml`, matching the PDS's networking pattern
  - Own `.env` for service-specific secrets, not stored in either memory system (same house rule as bsky-mcp's ADMIN_KEY)

## Resolved since

- **Login is real AT-Proto OAuth** (2026-08-03). The implementation had drifted from
  the decision below: it asked users to paste a bsky-mcp access token. Those tokens
  carry no account binding — `mcp_tokens` has no `did` column and bsky-mcp's
  `provider.ts` never mentions an account — so `bsky_whoami` fell through to
  `DEFAULT_ACCOUNT` and **every user resolved as `bob.pds.theblueai.org`**. The
  whiteboard could not tell two people apart, which made "text is single-owner"
  vacuous: there was only ever one owner. The backend now runs the OAuth flow
  (PAR + DPoP + PKCE) against the user's own PDS and issues its own session.
  Implementation: `backend/app/atproto_oauth.py`, `backend/app/oauth_routes.py`.

  Deliberate simplification: the whiteboard never acts on a user's behalf against
  their PDS, so OAuth is used *purely as an identity provider*. We verify once at
  login and mint our own opaque session — no AT-Proto token storage, no refresh, no
  DPoP-signed resource calls. Scope requested is `atproto` only, not
  `transition:generic`; the whiteboard cannot post or read as you.

- **Shape edit/move/delete rules** (2026-08-02): **free-for-all.** Anyone can move,
  resize or erase any mark — strokes and shapes alike. Text stays single-owner. This
  matches the skeleton's "no conflict resolution, you see the mess" and makes the
  update rule identical to the delete rule that was already in place: a mark you can
  erase is a mark you can move. Enforced in `db.update_element` and mirrored in the
  client so it doesn't send ops the server would reject.

- **Element storage format** (2026-08-02): Excalidraw elements are stored **verbatim**
  in `canvas_elements.data`. The backend never inspects them; partial elements from AI
  agents are completed server-side by `app/elements.py`. See CHANGES.md for why the
  previous translate-and-rebuild approach was retired.

## Still open (carried forward from the session — see full Q&A in chat history)

- Per-canvas participant model: any PDS account can join, or owner-curated invite list?
- AI trigger priority for v1: proposed **mention/tag + direct invitation only**; defer heartbeat/standing-watch to v2. Not yet confirmed.
- Should AI-authored strokes/text be visually marked (border/badge) or indistinguishable from human ones?
- Scale targets: how many canvases concurrently, how many users per canvas.
- FastAPI confirmation (or alternative Python framework).
- Whether bsky-mcp's existing OAuth client code can be reused directly for Whiteboard login, or needs its own OAuth client registration against the PDS.
