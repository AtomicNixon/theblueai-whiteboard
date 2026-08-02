# Changes — Synopses

Brief synopsis of the changes made to the Whiteboard (collaborative live canvas).
Focused on getting the app to a state where you can log in and test whiteboard features
end-to-end.

---

## Frontend

### `frontend/src/CanvasView.tsx` — live-sync rework (the main fix)

The old implementation used `initialData={{ elements }}` + React `setElements` to try to
push state into Excalidraw. `initialData` is only read **once** when the editor mounts, so
remote changes (from other users) never rendered at all. It has been rewritten to use the
imperative API:

- **Remote updates now render.** Elements are applied with
  `excalidrawAPI.updateScene({ elements, captureUpdate: CaptureUpdateAction.NEVER })`.
- **Duplicate element creation fixed.** Every drawn element was being created twice (an
  HTTP `POST` *and* a WebSocket `op:"add"`, each inserting a new DB row). Now creates go
  through the HTTP route only; the server's broadcast echo back-fills the backend id
  (`customData.wbid`) via an idempotent in-scene replacement.
- **Missing update sync added.** Moves, resizes and text edits now send `op:"update"`
  (owner-only text, matching the backend rule); deletions send `op:"delete"`. Changes are
  coalesced through a short (150 ms) debounce so a drag sends one update, not one per frame.
- **Delete side-effect removed.** Deletes are no longer fired from inside a React state
  updater (a StrictMode hazard that could double-send).
- **Echo-loop suppression.** Excalidraw normalizes elements in place and fires `onChange`
  asynchronously, so you can't tell a remote echo from a local edit by object identity.
  A `knownRef` baseline of each element's `versionNonce` is rebuilt synchronously right
  after every `applyScene`; remote echoes diff to nothing, while real local edits bump the
  version and are detected.
- **Ownership UX.** Other users' text elements are `locked` in the UI so you can't drag
  them (consistent with the backend's owner-only rule); marks stay freely erasable.
- **Race-safe flushing.** Pending create/update/delete batches are swapped out (not
  cleared) during a flush so items queued mid-flush aren't lost.

### `frontend/src/types.ts`

- Added `me` (the caller's DID) to `SnapshotOut` and the WS `snapshot` op, so the client
  can gate edits by ownership.

## Backend

- **`app/routes.py` / `app/ws_routes.py`**
  - Snapshot responses now include `me` (the caller's DID).
  - The AI wake (`@tag` mention post) now runs **in the background**
    (`schedule_ai_tagged`) instead of awaiting a 15 s HTTP call inline, so creating a
    tagged text element no longer blocks the response.
- **`app/ai_trigger.py`** — added `schedule_ai_tagged()` / a guarded background task that
  never propagates exceptions.
- **`app/auth.py`** — `validate_token()` now caches `token → {did, handle}` in memory with
  a 5-minute TTL (was 2 bsky-mcp round-trips per request).
- **`app/main.py`** — replaced the deprecated `@app.on_event("startup")` with a FastAPI
  lifespan. (CORS middleware preserved.)

## Verification

- `cd frontend && npm run build` — clean (strict `tsc` + Vite build).
- Backend: `python -m py_compile` on all `app/` files passes; the FastAPI app imports with
  all 15 routes; `ruff check app/` shows only two pre-existing `E501` line-length warnings
  in `db.py` (untouched).

## Known v1 limitations (accepted, per the KISS design)

- An element erased within ~150 ms of being drawn isn't persisted (debounce window).
- Dragging another user's mark moves it locally but does not persist (marks are immutable
  by design).
- No CRDT / no history: an erasure is permanent.
