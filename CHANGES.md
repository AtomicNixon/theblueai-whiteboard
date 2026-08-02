# Changes — Synopses

Brief synopsis of the changes made to the Whiteboard (collaborative live canvas).
Focused on getting the app to a state where you can log in and test whiteboard features
end-to-end.

---

## 2026-08-02 — Elements are stored verbatim (architecture change)

**The decision:** stop translating Excalidraw elements into a schema of our own.
Store what Excalidraw gives us, byte for byte, and let the backend stay ignorant of
what's inside.

**Why.** The old `excToBackend` / `backendToExc` pair in `CanvasView.tsx` decomposed
each element into ~10 fields and rebuilt it from scratch on the way back. That
reconstruction is where the rendering bugs lived, and it re-coupled us to Excalidraw's
internals on every release:

- `simulatePressure` was never reconstructed, so a freehand stroke that round-tripped
  through the server came back with an empty `pressures` array and rendered as a
  zero-width line. Locally-drawn strokes looked fine until the server echoed them —
  which is why this read as a sync bug rather than a serialization bug.
- `seed` was hardcoded to `1`, so every shape shared one roughness seed and the
  hand-drawn style collapsed into uniformity.
- `version` / `versionNonce` were hardcoded to `1`, fighting the `knownRef`
  change-detection baseline.

None of these were individually hard to fix. All of them were the same bug, and a
fourth one would have arrived with Excalidraw 0.19.

**What changed:**

- `frontend/src/CanvasView.tsx` — `excToBackend` is now a spread plus a three-field
  strip list; `backendToExc` is a spread plus re-attaching those three. 58 lines of
  translation deleted. The stripped fields are `locked` (computed per viewer — one
  user's lock state must not become everyone's), `isDeleted` (deletion is a row delete;
  a stored soft-delete is an invisible permanent ghost), and `customData` (holds
  `{wbid, owner}`, both authoritative columns on the row).
- `frontend/src/CanvasView.tsx` — `stampWbid` no longer mutates a scene element in
  place. `updateScene` reconciles by `version`/`versionNonce`, neither of which an
  in-place `customData` write bumps, so the stamp could be silently discarded — leaving
  the element with no backend id and making every later edit look like a fresh create.
- `backend/app/elements.py` **(new)** — `normalize(kind, data, element_id)`. Browser
  clients post complete elements and pass through untouched; AI agents post partials
  (`wb_add_text` sends only `{text, x, y, width, height}`) and get the required
  Excalidraw fields filled in. Never overwrites a field the client supplied.
  `strip_for_storage(data)` is the write-side counterpart for updates.
- `backend/app/routes.py`, `backend/app/ws_routes.py` — both add paths run `normalize`,
  both update paths run `strip_for_storage`.
- `backend/tests/test_elements.py` **(new)** — 8 tests. The load-bearing one is
  `test_browser_element_passes_through_untouched`: if that regresses, we're back in the
  translation layer.

**No DB migration.** `canvas_elements.data` was always a free-form JSONB column; the
client just wasn't using it that way.

### Marks are now free-for-all

The last open question from `WHITEBOARD_PLAN.md`. Marks — every non-text element:
freedraw, line, rectangle, ellipse, arrow, diamond — can be moved, resized and erased
by anyone, not just whoever drew them. Text stays single-owner.

Previously `handleChange` queued updates only for text you owned, so dragging a shape
moved it on your screen and it snapped back on reload. `db.update_element`'s predicate
becomes `(kind = 'mark' OR owner_did = $3)`, which is exactly the rule `delete_element`
already used — a mark you can erase is a mark you can move. The client mirrors the split
so it doesn't send ops the server would reject, and other users' text remains `locked`
in the Excalidraw UI while marks stay unlocked for everyone.

**Compatibility:** the bsky-mcp `wb_*` tools are unchanged and need no redeploy.
`wb_read_canvas` reads `element.data.text` / `.x` / `.y`, and Excalidraw text elements
carry exactly those field names.

**Verification:** `npm run build` clean (strict `tsc` + Vite). `ruff check app/` clean.
FastAPI app imports with all 15 routes. `python tests/test_elements.py` — 8/8 pass.

**Also:** a live `WB_WAKER_BSKY_TOKEN` had been pasted into `deploy/.env.example`, which
is a tracked template. Moved to `deploy/.env` (gitignored); the template is blank again.

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
- No CRDT / no history: an erasure is permanent.
- Two people dragging the same mark at once is last-write-wins, with visible flicker.
  This is the "no conflict resolution, you see the mess" rule working as designed, not
  a defect.
