import { useEffect, useState } from 'react'
import CanvasList from './CanvasList'
import CanvasView from './CanvasView'
import Accounts from './pages/Accounts'
import Help from './pages/Help'
import Home from './pages/Home'
import Who from './pages/Who'
import { ROUTES, go } from './pages/shared'
import type { CanvasOut } from './types'

const TOKEN_KEY = 'wb_token'
const HANDLE_KEY = 'wb_handle'

/**
 * Routing, such as it is.
 *
 * Four static pages plus the whiteboard itself. A router library would be more
 * than this needs — pathname plus popstate is the whole requirement, and the
 * backend already serves index.html for any unmatched path so deep links work.
 */
export default function App() {
  const [path, setPath] = useState(() => window.location.pathname)
  const [token, setToken] = useState(() => localStorage.getItem(TOKEN_KEY) ?? '')
  const [handle, setHandle] = useState(() => localStorage.getItem(HANDLE_KEY) ?? '')
  const [canvas, setCanvas] = useState<CanvasOut | null>(null)

  useEffect(() => {
    const onPop = () => setPath(window.location.pathname)
    window.addEventListener('popstate', onPop)
    return () => window.removeEventListener('popstate', onPop)
  }, [])

  function signedIn(session: string, who: string) {
    localStorage.setItem(TOKEN_KEY, session)
    localStorage.setItem(HANDLE_KEY, who)
    setToken(session)
    setHandle(who)
    go(ROUTES.whiteboard)
  }

  function logout() {
    const t = localStorage.getItem(TOKEN_KEY)
    if (t) {
      void fetch('/api/auth/logout', {
        method: 'POST', headers: { Authorization: `Bearer ${t}` },
      }).catch(() => {})
    }
    localStorage.removeItem(TOKEN_KEY)
    localStorage.removeItem(HANDLE_KEY)
    setToken('')
    setHandle('')
    setCanvas(null)
    go(ROUTES.home)
  }

  if (path === ROUTES.accounts) return <Accounts />
  if (path === ROUTES.help) return <Help />
  if (path === ROUTES.who) return <Who />

  if (path === ROUTES.whiteboard) {
    // Sending someone to a login screen with no explanation is how you lose
    // them; Home carries the "what is this" links.
    if (!token) return <Home onSignedIn={signedIn} />
    if (!canvas) {
      return <CanvasList token={token} handle={handle} onOpen={setCanvas} onLogout={logout} />
    }
    return (
      <CanvasView token={token} handle={handle} canvas={canvas}
                  onBack={() => setCanvas(null)} onLogout={logout} />
    )
  }

  return <Home onSignedIn={signedIn} />
}
