import { useEffect, useRef, useState } from 'react'
import { Excalidraw, CaptureUpdateAction } from '@excalidraw/excalidraw'
import type { ExcalidrawElement } from '@excalidraw/excalidraw/element/types'
import type { CanvasOut, ElementOut, SnapshotOut, WsOp } from './types'

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

function excToBackend(el: ExcalidrawElement): { kind: 'text' | 'mark'; data: Record<string, unknown> } {
  const common = {
    exid: el.id,
    x: el.x, y: el.y, width: el.width, height: el.height,
    angle: el.angle, strokeColor: el.strokeColor, backgroundColor: el.backgroundColor,
    strokeWidth: el.strokeWidth, opacity: el.opacity,
  }
  if (el.type === 'text') {
    return { kind: 'text', data: { ...common, text: el.text, fontSize: el.fontSize } }
  }
  // All non-text elements (freedraw, line, rectangle, ellipse, arrow, diamond)
  // are marks: append-only, free-for-all.
  return {
    kind: 'mark',
    data: { ...common, type: el.type, points: (el as any).points },
  }
}

function backendToExc(el: ElementOut, me?: string): ExcalidrawElement {
  const d = el.data
  const isText = el.kind === 'text'
  const base = {
    id: (d.exid as string) ?? el.id,
    x: (d.x as number) ?? 0,
    y: (d.y as number) ?? 0,
    width: (d.width as number) ?? 100,
    height: (d.height as number) ?? 100,
    angle: (d.angle as number) ?? 0,
    strokeColor: (d.strokeColor as string) ?? '#1e1e1e',
    backgroundColor: (d.backgroundColor as string) ?? 'transparent',
    fillStyle: 'hachure',
    strokeWidth: (d.strokeWidth as number) ?? 1,
    strokeStyle: 'solid',
    roughness: 1,
    opacity: (d.opacity as number) ?? 100,
    groupIds: [],
    frameId: null,
    roundness: null,
    seed: 1,
    version: 1,
    versionNonce: 1,
    isDeleted: false,
    boundElements: null,
    updated: Date.now(),
    link: null,
    // Lock other users' text so only the owner can drag/edit it (matches the
    // backend rule that text is owner-mutable). Marks stay unlocked so anyone
    // can still erase them.
    locked: isText && !!me && el.owner_did !== me,
    customData: { wbid: el.id, owner: el.owner_did },
  } as any
  if (isText) {
    return { ...base, type: 'text', text: (d.text as string) ?? '', fontSize: (d.fontSize as number) ?? 20,
      fontFamily: 1, textAlign: 'left', verticalAlign: 'top', baseline: 18, containerId: null,
      originalText: (d.text as string) ?? '', lineHeight: 1.25 }
  }
  return { ...base, type: (d.type as string) ?? 'freedraw', points: (d.points as [number, number][]) ?? [[0, 0]],
    lastCommittedPoint: null, pressures: [] as number[] } as ExcalidrawElement
}

// Minimal surface of the Excalidraw imperative API we use.
interface ExcAPI {
  updateScene: (opts: { elements: ExcalidrawElement[]; captureUpdate: string }) => void
  getSceneElements: () => ExcalidrawElement[]
}

interface KnownInfo { v: number; wbid?: string }

export default function CanvasView({
  token, canvas, onBack, onLogout,
}: { token: string; canvas: CanvasOut; onBack: () => void; onLogout: () => void }) {
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
      for (const [exid, el] of pend.creates) {
        if (!alive.has(exid)) continue // drawn then erased before flush
        const { kind, data } = excToBackend(el)
        try {
          const created = await api<ElementOut>(`/canvases/${canvas.id}/elements`, token, {
            method: 'POST', body: JSON.stringify({ kind, data }),
          })
          stampWbid(exid, created.id, created.owner_did)
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
    let changed = false
    for (const e of scene) {
      if (e.id === exid) {
        ;(e as any).customData = { wbid, owner }
        changed = true
        break
      }
    }
    if (changed) applyScene(scene)
  }

  function handleChange(next: readonly ExcalidrawElement[]) {
    const arr = next as ExcalidrawElement[]
    const pend = pendingRef.current
    const seen = new Set<string>()

    for (const el of arr) {
      seen.add(el.id)
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
        // Local edit. Text is owner-mutable; marks are append-only.
        if (wbid && el.type === 'text' && (el as any).customData?.owner === myDidRef.current) {
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
        <span style={{ color: '#888' }}>{canvas.id}</span>
        {err && <span style={{ color: 'crimson' }}>{err}</span>}
        <div style={{ flex: 1 }} />
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
