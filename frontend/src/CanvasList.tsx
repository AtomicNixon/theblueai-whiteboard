import { useEffect, useState } from 'react'
import type { CanvasOut } from './types'

async function api<T>(path: string, token: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`/api${path}`, {
    ...init,
    headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json', ...(init?.headers ?? {}) },
  })
  if (!res.ok) throw new Error(`${res.status} ${await res.text()}`)
  return res.json() as Promise<T>
}

export default function CanvasList({
  token, handle, onOpen, onLogout,
}: { token: string; handle?: string; onOpen: (c: CanvasOut) => void; onLogout: () => void }) {
  const [canvases, setCanvases] = useState<CanvasOut[]>([])
  const [title, setTitle] = useState('')
  const [joinId, setJoinId] = useState('')
  const [err, setErr] = useState('')

  async function load() {
    try {
      setCanvases(await api<CanvasOut[]>('/canvases', token))
      setErr('')
    } catch (e) {
      setErr(String(e))
    }
  }

  useEffect(() => { void load() }, [])

  async function create() {
    if (!title.trim()) return
    try {
      const c = await api<CanvasOut>('/canvases', token, {
        method: 'POST', body: JSON.stringify({ title: title.trim() }),
      })
      onOpen(c)
    } catch (e) {
      setErr(String(e))
    }
  }

  // Opening someone else's canvas by id records you as a member, so it shows
  // up in your list from then on.
  async function join() {
    const id = joinId.trim()
    if (!id) return
    try {
      onOpen(await api<CanvasOut>(`/canvases/${id}`, token))
    } catch {
      setErr(`No canvas with id ${id}`)
    }
  }

  return (
    <div style={{ maxWidth: 720, margin: '2rem auto', padding: '0 1rem', fontFamily: 'system-ui' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <h1 style={{ marginBottom: 0 }}>Your canvases</h1>
        <div style={{ display: 'flex', gap: 12, alignItems: 'center' }}>
          {handle && <span style={{ color: '#888', fontSize: 14 }}>{handle}</span>}
          <button onClick={onLogout}>Log out</button>
        </div>
      </div>
      {err && <div style={{ color: 'crimson' }}>{err}</div>}
      <div style={{ display: 'flex', gap: 8, marginBottom: 8, marginTop: 16 }}>
        <input
          placeholder="New canvas title"
          value={title}
          onChange={(e) => setTitle(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter') void create() }}
          style={{ flex: 1, padding: 8 }}
        />
        <button onClick={create} style={{ padding: '8px 16px' }}>Create</button>
      </div>
      <div style={{ display: 'flex', gap: 8, marginBottom: 20 }}>
        <input
          placeholder="…or open a canvas by id someone shared with you"
          value={joinId}
          onChange={(e) => setJoinId(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter') void join() }}
          style={{ flex: 1, padding: 8 }}
          autoCapitalize="none"
          spellCheck={false}
        />
        <button onClick={join} style={{ padding: '8px 16px' }}>Open</button>
      </div>
      {canvases.length === 0 ? (
        <p style={{ color: '#666' }}>
          No canvases yet. Create one, or open one by id.
        </p>
      ) : (
        <ul style={{ listStyle: 'none', padding: 0 }}>
          {canvases.map((c) => (
            <li key={c.id} style={{ padding: 12, borderBottom: '1px solid #eee', cursor: 'pointer' }}
                onClick={() => onOpen(c)}>
              <strong>{c.title || '(untitled)'}</strong>
              <span style={{ color: '#888', marginLeft: 12 }}>{new Date(c.created_at).toLocaleString()}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
