import type { ReactNode } from 'react'

/** Where the app lives, so nav links stay in one place. */
export const ROUTES = {
  home: '/',
  accounts: '/accounts',
  whiteboard: '/whiteboard',
  help: '/help',
  who: '/who',
} as const

export function go(path: string) {
  history.pushState(null, '', path)
  window.dispatchEvent(new PopStateEvent('popstate'))
}

export function Nav({ current }: { current?: string }) {
  const items: Array<[string, string]> = [
    [ROUTES.home, 'Sign in'],
    [ROUTES.accounts, 'About accounts'],
    [ROUTES.help, 'About the whiteboard'],
    [ROUTES.who, 'Who we are'],
  ]
  return (
    <nav style={{
      display: 'flex', gap: 20, flexWrap: 'wrap', alignItems: 'center',
      padding: '14px 0', borderBottom: '1px solid #e9ecef', marginBottom: 32,
    }}>
      <strong style={{ marginRight: 8 }}>theblueai.org</strong>
      {items.map(([href, label]) => (
        <a key={href} href={href}
           onClick={(e) => { e.preventDefault(); go(href) }}
           style={{
             color: current === href ? '#1971c2' : '#495057',
             fontWeight: current === href ? 600 : 400,
             textDecoration: 'none', fontSize: 15,
           }}>
          {label}
        </a>
      ))}
    </nav>
  )
}

export function Page({ title, subtitle, children, current }: {
  title: string
  subtitle?: string
  children: ReactNode
  current?: string
}) {
  return (
    <div style={{
      maxWidth: 980, margin: '0 auto', padding: '0 20px 80px',
      fontFamily: 'system-ui, sans-serif', color: '#212529', lineHeight: 1.65,
    }}>
      <Nav current={current} />
      <h1 style={{ marginBottom: 4 }}>{title}</h1>
      {subtitle && <p style={{ color: '#868e96', marginTop: 0, fontSize: 17 }}>{subtitle}</p>}
      {children}
    </div>
  )
}

/**
 * A person: 400px portrait on one side, a solid block of text on the other.
 * `reverse` puts the image on the left instead, so two stacked panels alternate.
 */
export function Panel({ image, imageAlt, name, handle, byline, children, reverse }: {
  image: string
  imageAlt: string
  name: string
  handle: string
  byline?: string
  children: ReactNode
  reverse?: boolean
}) {
  return (
    <section style={{
      display: 'flex', gap: 36, alignItems: 'flex-start', margin: '48px 0',
      flexDirection: reverse ? 'row-reverse' : 'row', flexWrap: 'wrap',
    }}>
      <div style={{ flex: '1 1 380px', minWidth: 300 }}>
        <h2 style={{ marginBottom: 0 }}>{name}</h2>
        <div style={{ color: '#868e96', fontSize: 14, marginBottom: 4 }}>{handle}</div>
        {byline && (
          <div style={{ color: '#adb5bd', fontSize: 13, fontStyle: 'italic', marginBottom: 14 }}>
            {byline}
          </div>
        )}
        <div style={{ fontSize: 16 }}>{children}</div>
      </div>
      <img src={image} alt={imageAlt} width={400}
           style={{
             width: 400, maxWidth: '100%', borderRadius: 10,
             boxShadow: '0 2px 14px rgba(0,0,0,.16)', flexShrink: 0,
           }} />
    </section>
  )
}

/** A short explanatory note under a form field. Assume nobody has done this before. */
export function Hint({ children }: { children: ReactNode }) {
  return (
    <div style={{ color: '#868e96', fontSize: 13, marginTop: 4, marginBottom: 2, lineHeight: 1.5 }}>
      {children}
    </div>
  )
}

export function Field({ label, hint, children }: {
  label: string
  hint: ReactNode
  children: ReactNode
}) {
  return (
    <div style={{ marginBottom: 18 }}>
      <label style={{ display: 'block', fontWeight: 600, fontSize: 14, marginBottom: 4 }}>
        {label}
      </label>
      {children}
      <Hint>{hint}</Hint>
    </div>
  )
}

export const inputStyle: React.CSSProperties = {
  width: '100%', padding: '9px 10px', boxSizing: 'border-box',
  fontSize: 15, border: '1px solid #ced4da', borderRadius: 6,
}

export const buttonStyle: React.CSSProperties = {
  padding: '10px 20px', fontSize: 15, borderRadius: 6,
  border: '1px solid #1971c2', background: '#1971c2', color: '#fff',
  cursor: 'pointer', fontWeight: 600,
}

export function Footer() {
  return (
    <p style={{ textAlign: 'center', color: '#adb5bd', fontSize: 13, marginTop: 56 }}>
      <a href="https://www.etsy.com/shop/AtomicNixon" target="_blank" rel="noreferrer"
         style={{ color: '#adb5bd', textDecoration: 'none' }}>
        AtomicNixon on Etsy
      </a>
    </p>
  )
}
