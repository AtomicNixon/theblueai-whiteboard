import { useEffect, useState } from 'react'
import CanvasList from './CanvasList'
import CanvasView from './CanvasView'
import type { CanvasOut } from './types'

const TOKEN_KEY = 'wb_token'
const HANDLE_KEY = 'wb_handle'

/**
 * Sign in with an AT Protocol account on theblueai.org.
 *
 * The old flow asked for a pasted bsky-mcp access token. Those tokens carry no
 * account binding, so the backend resolved every one of them to bsky-mcp's
 * default account — meaning the whiteboard literally could not tell two users
 * apart, and "text is single-owner" was vacuous because there was only ever one
 * owner. Now the backend runs a real OAuth flow against the user's PDS and
 * issues its own session token.
 *
 * The session token comes back in the URL fragment (`#session=...`) rather than
 * a query parameter: fragments aren't sent to servers, so it stays out of
 * access logs and Referer headers. We stash it and strip it from the URL
 * immediately so a copied link never carries a live session.
 */
export default function App() {
  const [token, setToken] = useState<string>(() => localStorage.getItem(TOKEN_KEY) ?? '')
  const [handle, setHandle] = useState<string>(() => localStorage.getItem(HANDLE_KEY) ?? '')
  const [handleInput, setHandleInput] = useState('')
  const [canvas, setCanvas] = useState<CanvasOut | null>(null)
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)

  // Pick up the result of an OAuth redirect.
  useEffect(() => {
    const hash = window.location.hash
    if (!hash || hash.length < 2) return
    const params = new URLSearchParams(hash.slice(1))

    const session = params.get('session')
    const error = params.get('error')
    if (session) {
      localStorage.setItem(TOKEN_KEY, session)
      setToken(session)
      void fetchMe(session)
    } else if (error) {
      setErr(decodeURIComponent(error))
    }
    if (session || error) {
      history.replaceState(null, '', window.location.pathname + window.location.search)
    }
  }, [])

  async function fetchMe(t: string) {
    try {
      const res = await fetch('/api/auth/me', { headers: { Authorization: `Bearer ${t}` } })
      if (!res.ok) return
      const me = (await res.json()) as { did: string; handle: string }
      localStorage.setItem(HANDLE_KEY, me.handle)
      setHandle(me.handle)
    } catch {
      /* non-fatal — the handle is cosmetic, the token is what matters */
    }
  }

  async function signIn() {
    const h = handleInput.trim().replace(/^@/, '')
    if (!h) return
    setBusy(true)
    setErr('')
    try {
      const res = await fetch('/api/auth/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ handle: h }),
      })
      const body = await res.json()
      if (!res.ok) throw new Error(body.detail ?? `sign-in failed (${res.status})`)
      // Hand off to the PDS's consent screen.
      window.location.href = body.redirect_url
    } catch (e) {
      setErr(String(e instanceof Error ? e.message : e))
      setBusy(false)
    }
  }

  function logout() {
    const t = localStorage.getItem(TOKEN_KEY)
    if (t) {
      void fetch('/api/auth/logout', {
        method: 'POST',
        headers: { Authorization: `Bearer ${t}` },
      }).catch(() => {})
    }
    localStorage.removeItem(TOKEN_KEY)
    localStorage.removeItem(HANDLE_KEY)
    setToken('')
    setHandle('')
    setCanvas(null)
  }

  if (!token) {
    return (
      <div style={{ maxWidth: 460, margin: '4rem auto', padding: '0 1rem', fontFamily: 'system-ui' }}>
        <h1 style={{ marginBottom: 4 }}>Whiteboard</h1>
        <p style={{ color: '#666', marginTop: 0 }}>
          A shared canvas for humans and AIs. Sign in with your theblueai.org account.
        </p>

        <label htmlFor="handle" style={{ display: 'block', fontSize: 14, marginBottom: 4 }}>
          Handle
        </label>
        <input
          id="handle"
          type="text"
          placeholder="you.pds.theblueai.org"
          value={handleInput}
          onChange={(e) => setHandleInput(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter') void signIn() }}
          disabled={busy}
          style={{ width: '100%', padding: 8, boxSizing: 'border-box', fontSize: 15 }}
          autoFocus
          autoCapitalize="none"
          autoCorrect="off"
          spellCheck={false}
        />

        <button
          onClick={() => void signIn()}
          disabled={busy || !handleInput.trim()}
          style={{ marginTop: 10, padding: '8px 18px', fontSize: 15 }}
        >
          {busy ? 'Redirecting…' : 'Sign in'}
        </button>

        {err && (
          <p style={{ color: 'crimson', marginTop: 12, fontSize: 14, wordBreak: 'break-word' }}>
            {err}
          </p>
        )}

        <p style={{ color: '#999', fontSize: 12, marginTop: 28, lineHeight: 1.5 }}>
          You'll be sent to your PDS to approve access, then returned here. The
          whiteboard only learns who you are — it never gets the ability to post
          or read on your behalf.
        </p>
      </div>
    )
  }

  if (!canvas) {
    return <CanvasList token={token} handle={handle} onOpen={setCanvas} onLogout={logout} />
  }

  return (
    <CanvasView
      token={token}
      handle={handle}
      canvas={canvas}
      onBack={() => setCanvas(null)}
      onLogout={logout}
    />
  )
}
