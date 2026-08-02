import { useState } from 'react'
import CanvasList from './CanvasList'
import CanvasView from './CanvasView'
import type { CanvasOut } from './types'

const TOKEN_KEY = 'wb_token'

export default function App() {
  const [token, setToken] = useState<string>(() => localStorage.getItem(TOKEN_KEY) ?? '')
  const [tokenInput, setTokenInput] = useState('')
  const [canvas, setCanvas] = useState<CanvasOut | null>(null)

  function login() {
    const t = tokenInput.trim()
    if (!t) return
    localStorage.setItem(TOKEN_KEY, t)
    setToken(t)
    setTokenInput('')
  }

  function logout() {
    localStorage.removeItem(TOKEN_KEY)
    setToken('')
    setCanvas(null)
  }

  if (!token) {
    return (
      <div style={{ maxWidth: 480, margin: '4rem auto', padding: '0 1rem', fontFamily: 'system-ui' }}>
        <h1>Whiteboard</h1>
        <p>Log in with your bsky-mcp access token (your theblueai.org PDS account).</p>
        <input
          type="password"
          placeholder="bsky-mcp access token"
          value={tokenInput}
          onChange={(e) => setTokenInput(e.target.value)}
          style={{ width: '100%', padding: 8, boxSizing: 'border-box' }}
          autoFocus
        />
        <button onClick={login} style={{ marginTop: 8, padding: '8px 16px' }}>Connect</button>
      </div>
    )
  }

  if (!canvas) {
    return <CanvasList token={token} onOpen={setCanvas} onLogout={logout} />
  }

  return <CanvasView token={token} canvas={canvas} onBack={() => setCanvas(null)} onLogout={logout} />
}
