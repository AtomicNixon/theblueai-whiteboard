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
  token, onOpen, onLogout,
}: { token: string; onOpen: (c: CanvasOut) => void; onLogout: () => void }) {
  const [canvases, setCanvases] = useState<CanvasOut[]>([])
  const [title, setTitle] = useState('')
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

  return (
    <div style={{ maxWidth: 720, margin: '2rem auto', padding: '0 1rem', fontFamily: 'system-ui' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <h1>Your canvases</h1>
        <button onClick={onLogout}>Log out</button>
      </div>
      {err && <div style={{ color: 'crimson' }}>{err}</div>}
      <div style={{ display: 'flex', gap: 8, marginBottom: 16 }}>
        <input
          placeholder="New canvas title"
          value={title}
          onChange={(e) => setTitle(e.target.value)}
          style={{ flex: 1, padding: 8 }}
        />
        <button onClick={create} style={{ padding: '8px 16px' }}>Create</button>
      </div>
      {canvases.length === 0 ? (
        <p>No canvases yet.</p>
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
