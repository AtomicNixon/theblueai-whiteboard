import { useEffect, useRef, useState } from 'react'
import { Excalidraw, CaptureUpdateAction } from '@excalidraw/excalidraw'
import type { ExcalidrawElement } from '@excalidraw/excalidraw/element/types'
import type { CanvasOut, ElementOut, SnapshotOut, WsOp } from './types'
import { vectorizeImage } from './vectorize'

/**
 * CanvasView wires Excalidraw (drawing surface) to our own WebSocket transport.
 *
 * Excalidraw owns the in-memory element list. We sync deltas:
 *  - Initial load: HTTP snapshot -> applied via the imperative `updateScene` API.
 *  - Remote change (someone else drew): WS op arrives -> we merge it into the
 *    current scene and call `updateScene({ captureUpdate: NEVER })`.
 *  - Local change (user draws/edits): Excalidraw drives the scene; our
 *    `onChange` diffs against a versionNonce baseline and sends the op.
 *
 * Echo suppression: `updateScene` normalizes elements in place (assigns `index`,
 * bumps `versionNonce`) and `onChange` fires asynchronously afterwards. We can't
 * tell remote from local by array reference (Excalidraw mutates in place). So we
 * keep `knownRef` = the versionNonce baseline, rebuilt synchronously right after
 * every `applyScene`. The onChange echo of a remote apply then diffs to nothing,
 * while a real local edit bumps versionNonce and is detected.
 *
 * Element mapping:
 *  - Excalidraw text element  -> backend kind="text" (owned, mutable by owner)
 *  - Excalidraw stroke/shape  -> backend kind="mark" (append-only, free-for-all)
 *
 * We carry our backend element id in the Excalidraw element's `customData`
 * field so we can correlate updates/deletes.
 */
const API = '/api'
const FLUSH_MS = 150

async function api<T>(path: string, token: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API}${path}`, {
    ...init,
    headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json', ...(init?.headers ?? {}) },
  })
  if (!res.ok) throw new Error(`${res.status} ${await res.text()}`)
  return res.json() as Promise<T>
}

/**
 * Fields we never send to the server.
 *  - `locked` is computed per viewer (other users' text is locked in *your* UI),
 *    so persisting one viewer's lock state would impose it on everyone.
 *  - `isDeleted` — deletion is a row delete on the backend; a stored soft-delete
 *    would be an invisible element that never goes away.
 *  - `customData` holds {wbid, owner}, both authoritative columns on the row.
 */
const STRIPPED = ['locked', 'isDeleted', 'customData'] as const

/**
 * Carry the Excalidraw element verbatim.
 *
 * We deliberately do NOT project it onto a schema of our own. Excalidraw's
 * serialized element format is stable and public; decomposing it and rebuilding
 * it on the way back is what silently dropped `simulatePressure` (zero-width
 * freehand strokes) and pinned every element to `seed: 1`. The backend only
 * needs `kind` to enforce ownership — it never looks inside `data`.
 */
function excToBackend(el: ExcalidrawElement): { kind: 'text' | 'mark'; data: Record<string, unknown> } {
  const data = { ...el } as Record<string, unknown>
  for (const k of STRIPPED) delete data[k]
  // Text is single-owner and mutable by its owner. Everything else — freedraw,
  // line, rectangle, ellipse, arrow, diamond — is a mark: append-only,
  // free-for-all.
  return { kind: el.type === 'text' ? 'text' : 'mark', data }
}

/** Re-attach the per-viewer and authoritative fields stripped on the way up. */
function backendToExc(el: ElementOut, me?: string): ExcalidrawElement {
  return {
    ...el.data,
    isDeleted: false,
    // Lock other users' text so only the owner can drag/edit it (matches the
    // backend rule that text is owner-mutable). Marks stay unlocked for
    // everyone — anyone can move, resize or erase any stroke or shape.
    locked: el.kind === 'text' && !!me && el.owner_did !== me,
    customData: { wbid: el.id, owner: el.owner_did },
  } as unknown as ExcalidrawElement
}

// Minimal surface of the Excalidraw imperative API we use.
interface ExcAPI {
  updateScene: (opts: { elements: ExcalidrawElement[]; captureUpdate: string }) => void
  getSceneElements: () => ExcalidrawElement[]
}

/** Excalidraw holds image binary here, keyed by the element's `fileId`. */
interface BinaryFile { id: string; mimeType: string; dataURL: string }
type BinaryFiles = Record<string, BinaryFile>

/** Element budget for a dropped image. See vectorize.ts for why we redraw
 *  pictures instead of storing them. */
const IMAGE_ELEMENTS = 250

interface KnownInfo { v: number; wbid?: string }

export default function CanvasView({
  token, handle, canvas, onBack, onLogout,
}: {
  token: string
  handle?: string
  canvas: CanvasOut
  onBack: () => void
  onLogout: () => void
}) {
  const [err, setErr] = useState('')
  const excalidrawAPIRef = useRef<ExcAPI | null>(null)
  const wsRef = useRef<WebSocket | null>(null)
  const myDidRef = useRef<string | undefined>(undefined)
  const pendingInitialRef = useRef<ExcalidrawElement[] | null>(null)
  // versionNonce baseline for change detection (id -> last recorded version).
  const knownRef = useRef<Map<string, KnownInfo>>(new Map())
  const pendingRef = useRef({
    creates: new Map<string, ExcalidrawElement>(),
    updates: new Map<string, ExcalidrawElement>(),
    deletes: new Set<string>(),
  })
  const flushTimer = useRef<number | null>(null)
  const disposedRef = useRef(false)
  // Image elements already converted, so onChange doesn't re-run the quadtree
  // on every subsequent event.
  const imagesHandledRef = useRef<Set<string>>(new Set())

  // Apply a full element list to the scene (snapshot/remote ops/stamping wbid).
  // After updateScene normalizes elements, rebuild the baseline so the onChange
  // echo of this apply diffs to nothing.
  function applyScene(elements: ExcalidrawElement[]) {
    const api = excalidrawAPIRef.current
    if (!api) {
      pendingInitialRef.current = elements
      return
    }
    api.updateScene({ elements, captureUpdate: CaptureUpdateAction.NEVER })
    knownRef.current.clear()
    for (const el of api.getSceneElements()) {
      knownRef.current.set(el.id, {
        v: el.versionNonce,
        wbid: (el as any).customData?.wbid as string | undefined,
      })
    }
  }

  function scheduleFlush() {
    if (flushTimer.current !== null) window.clearTimeout(flushTimer.current)
    flushTimer.current = window.setTimeout(() => {
      flushTimer.current = null
      void flush()
    }, FLUSH_MS)
  }

  async function flush() {
    if (disposedRef.current) return
    // Swap out so items queued during this flush aren't clobbered.
    const pend = pendingRef.current
    pendingRef.current = { creates: new Map(), updates: new Map(), deletes: new Set() }

    const editorApi = excalidrawAPIRef.current
    if (pend.creates.size) {
      const scene = editorApi?.getSceneElements() ?? []
      const alive = new Set(scene.map((e) => e.id))
      const batch: Array<[string, { kind: string; data: Record<string, unknown> }]> = []
      for (const [exid, el] of pend.creates) {
        if (!alive.has(exid)) continue // drawn then erased before flush
        batch.push([exid, excToBackend(el)])
      }

      if (batch.length === 1) {
        const [exid, payload] = batch[0]
        try {
          const created = await api<ElementOut>(`/canvases/${canvas.id}/elements`, token, {
            method: 'POST', body: JSON.stringify(payload),
          })
          stampWbid(exid, created.id, created.owner_did)
        } catch (e) { setErr(String(e)) }
      } else if (batch.length > 1) {
        // A vectorized image is hundreds of rectangles at once; one request
        // each would be hundreds of round trips for a single paste.
        try {
          const created = await api<ElementOut[]>(
            `/canvases/${canvas.id}/elements/bulk`, token,
            { method: 'POST', body: JSON.stringify({ elements: batch.map(([, p]) => p) }) },
          )
          created.forEach((c, i) => {
            const exid = batch[i]?.[0]
            if (exid) stampWbid(exid, c.id, c.owner_did)
          })
        } catch (e) { setErr(String(e)) }
      }
    }

    const ws = wsRef.current
    if (ws && ws.readyState === WebSocket.OPEN) {
      for (const [wbid, el] of pend.updates) {
        const { data } = excToBackend(el)
        ws.send(JSON.stringify({ op: 'update', element_id: wbid, data }))
      }
      for (const wbid of pend.deletes) {
        ws.send(JSON.stringify({ op: 'delete', element_id: wbid }))
      }
    }
  }

  // After a successful create, tag the live scene element with its backend id
  // so future edits are sent as updates, not re-creates.
  function stampWbid(exid: string, wbid: string, owner: string) {
    const api = excalidrawAPIRef.current
    if (!api) return
    const scene = api.getSceneElements()
    const idx = scene.findIndex((e) => e.id === exid)
    if (idx < 0) return
    // Replace with a fresh object rather than mutating in place. updateScene
    // reconciles by version/versionNonce, and an in-place `customData` change
    // bumps neither — so the mutation could be silently discarded, leaving the
    // element without its wbid and making every later edit look like a create.
    const next = scene.slice()
    next[idx] = { ...scene[idx], customData: { wbid, owner } } as ExcalidrawElement
    applyScene(next)
  }

  /**
   * A dropped or pasted image becomes a few hundred rectangles.
   *
   * We never store pictures — see vectorize.ts. The image element is removed
   * from the scene and replaced by its approximation, which is made of ordinary
   * marks and therefore syncs and persists with no special handling. Every
   * rectangle shares a groupId, so it still behaves like one object.
   */
  async function vectorizeAndReplace(el: ExcalidrawElement, file: BinaryFile) {
    const api = excalidrawAPIRef.current
    if (!api) return
    try {
      const rects = await vectorizeImage(
        file.dataURL,
        { x: el.x, y: el.y, width: el.width, height: el.height },
        { maxElements: IMAGE_ELEMENTS },
      )
      if (disposedRef.current) return

      // Drop the image element, add the approximation. These are brand-new
      // local elements with no wbid, so the normal flush persists them.
      const scene = api.getSceneElements().filter((e) => e.id !== el.id)
      applyScene([...scene, ...(rects as unknown as ExcalidrawElement[])])
    } catch (e) {
      setErr(`could not convert that image: ${e instanceof Error ? e.message : String(e)}`)
      // Leave the image in place rather than silently eating it; it won't
      // persist, but the user can see that something went wrong.
    }
  }

  function handleChange(next: readonly ExcalidrawElement[], _state: unknown, files?: BinaryFiles) {
    const arr = next as ExcalidrawElement[]
    const pend = pendingRef.current
    const seen = new Set<string>()

    for (const el of arr) {
      seen.add(el.id)

      // Images are converted, never stored. Excalidraw adds the element before
      // its binary finishes loading, so wait for the file to appear (onChange
      // fires again when it does).
      if (el.type === 'image' && !el.isDeleted) {
        const fileId = (el as unknown as { fileId?: string }).fileId
        const file = fileId && files ? files[fileId] : undefined
        if (file?.dataURL && !imagesHandledRef.current.has(el.id)) {
          imagesHandledRef.current.add(el.id)
          void vectorizeAndReplace(el, file)
        }
        continue // never queue an image element for the server
      }
      const info = knownRef.current.get(el.id)
      const wbid = (el as any).customData?.wbid as string | undefined
      if (el.isDeleted) {
        if (info?.wbid) pend.deletes.add(info.wbid)
        knownRef.current.delete(el.id)
        continue
      }
      if (!info) {
        // Brand-new local element not yet persisted.
        if (!wbid) pend.creates.set(el.id, el)
        knownRef.current.set(el.id, { v: el.versionNonce, wbid })
        continue
      }
      if (el.versionNonce !== info.v) {
        // Local edit. Text is owner-mutable — someone else's text box is yours
        // to read, not to move. Marks are free-for-all: anyone can drag, resize
        // or erase any stroke or shape. Chaos is a feature (whiteboard_skeleton.md).
        // The backend enforces the same split; this just avoids sending ops it
        // would reject.
        const mine = (el as any).customData?.owner === myDidRef.current
        if (wbid && (el.type !== 'text' || mine)) {
          pend.updates.set(wbid, el)
        }
        knownRef.current.set(el.id, { v: el.versionNonce, wbid })
      }
    }

    // Deletions that fully removed the element (Excalidraw soft-deletes with
    // isDeleted, so this is mostly a safety net).
    for (const [id, info] of knownRef.current) {
      if (!seen.has(id)) {
        if (info.wbid) pend.deletes.add(info.wbid)
        knownRef.current.delete(id)
      }
    }

    if (pend.creates.size || pend.updates.size || pend.deletes.size) scheduleFlush()
  }

  useEffect(() => {
    let cancelled = false
    disposedRef.current = false

    api<SnapshotOut>(`/canvases/${canvas.id}/snapshot`, token)
      .then((snap) => {
        if (cancelled) return
        myDidRef.current = snap.me
        applyScene(snap.elements.map((e) => backendToExc(e, snap.me)))
      })
      .catch((e) => { if (!cancelled) setErr(String(e)) })

    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:'
    const ws = new WebSocket(`${proto}//${location.host}/ws/canvas/${canvas.id}?token=${encodeURIComponent(token)}`)
    wsRef.current = ws
    ws.onmessage = (ev) => {
      const op = JSON.parse(ev.data) as WsOp
      const editorApi = excalidrawAPIRef.current
      if (!editorApi) return
      if (op.op === 'snapshot') {
        myDidRef.current = op.me
        applyScene(op.elements.map((e) => backendToExc(e, op.me)))
        return
      }
      if (op.op === 'add_bulk') {
        // Someone dropped an image; it arrives as one batch of rectangles.
        const scene = editorApi.getSceneElements()
        const byWbid = new Map(scene.map((p) => [(p as any).customData?.wbid, p]))
        const incoming = op.elements
          .filter((e) => !byWbid.has(e.id))
          .map((e) => backendToExc(e, myDidRef.current))
        if (incoming.length) applyScene([...scene, ...incoming])
        return
      }
      if (op.op === 'add' || op.op === 'update') {
        const exc = backendToExc(op.element, myDidRef.current)
        const scene = editorApi.getSceneElements()
        const idx = scene.findIndex((p) => (p as any).customData?.wbid === op.element.id || p.id === exc.id)
        const merged = idx >= 0 ? scene.map((p, i) => (i === idx ? exc : p)) : [...scene, exc]
        applyScene(merged)
        return
      }
      if (op.op === 'delete') {
        const wbid = op.element_id
        const merged = editorApi.getSceneElements().filter((p) => (p as any).customData?.wbid !== wbid && p.id !== wbid)
        applyScene(merged)
      }
    }
    ws.onerror = () => { if (!cancelled) setErr('websocket error') }

    return () => {
      cancelled = true
      disposedRef.current = true
      if (flushTimer.current !== null) window.clearTimeout(flushTimer.current)
      ws.close()
      wsRef.current = null
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [canvas.id, token])

  function onExcalidrawReady(api: any) {
    excalidrawAPIRef.current = api
    if (pendingInitialRef.current) {
      const els = pendingInitialRef.current
      pendingInitialRef.current = null
      applyScene(els)
    }
  }

  return (
    <div style={{ height: '100vh', display: 'flex', flexDirection: 'column' }}>
      <div style={{ padding: '8px 12px', borderBottom: '1px solid #ddd', display: 'flex', gap: 12, alignItems: 'center' }}>
        <button onClick={onBack}>← Back</button>
        <strong>{canvas.title || '(untitled)'}</strong>
        {/* The id is how you invite someone: they paste it into "open by id". */}
        <span
          style={{ color: '#888', cursor: 'pointer', fontFamily: 'monospace', fontSize: 13 }}
          title="Click to copy — share this id to invite someone"
          onClick={() => void navigator.clipboard?.writeText(canvas.id)}
        >
          {canvas.id}
        </span>
        {err && <span style={{ color: 'crimson' }}>{err}</span>}
        <div style={{ flex: 1 }} />
        {handle && <span style={{ color: '#888', fontSize: 14 }}>{handle}</span>}
        <button onClick={onLogout}>Log out</button>
      </div>
      <div style={{ flex: 1 }}>
        <Excalidraw
          excalidrawAPI={onExcalidrawReady}
          initialData={{ elements: [] }}
          onChange={handleChange}
          UIOptions={{ canvasActions: { loadScene: false, saveToActiveFile: false, export: false } }}
        />
      </div>
    </div>
  )
}
