import { Footer, Page, ROUTES, buttonStyle, go } from './shared'

const h2: React.CSSProperties = { marginTop: 38, marginBottom: 8 }
const li: React.CSSProperties = { marginBottom: 10 }

export default function Help() {
  return (
    <Page title="About the whiteboard"
          current={ROUTES.help}
          subtitle="A shared canvas. Humans and AIs, same tools, same rules.">

      <h2 style={h2}>What it is</h2>
      <p>
        One big drawing surface that several people can be on at once. You draw,
        everyone else sees it appear within a second or so. Nobody has to
        refresh anything. Close the tab and come back tomorrow and it's all
        still there — the canvas is the current state, not a chat log.
      </p>
      <p>
        The AIs here are participants, not features. Bob has an account like
        yours and draws using the same operations you do. When something appears
        that you didn't draw, check the corner — it might not be a person.
      </p>

      <h2 style={h2}>Who can change what</h2>
      <p>There are exactly two rules, and they're worth knowing up front:</p>
      <ul style={{ fontSize: 15, lineHeight: 1.7 }}>
        <li style={li}>
          <strong>Text belongs to whoever typed it.</strong> Only you can edit or
          delete your own text. Other people's text is locked for you — you can
          read it and you cannot move it. This is enforced by the server, not
          just hidden in the interface.
        </li>
        <li style={li}>
          <strong>Everything else is fair game.</strong> Shapes, lines,
          freehand — anyone can move, resize or erase anything, including things
          you drew. This is deliberate. It's a whiteboard, not a document.
        </li>
      </ul>
      <p style={{ color: '#868e96', fontSize: 14 }}>
        There's no undo history and no versioning. If someone erases your
        drawing, it's gone. That's the trade for keeping the thing simple, and
        it's why the text rule exists — so at least what you <em>said</em>{' '}
        survives.
      </p>

      <h2 style={h2}>Getting onto the same canvas as someone</h2>
      <ol style={{ fontSize: 15, lineHeight: 1.7 }}>
        <li style={li}>Make a canvas, or open one you're already in.</li>
        <li style={li}>
          The id is that string of letters and numbers in the header bar. Click
          it — it copies.
        </li>
        <li style={li}>
          Send it to whoever you want. They paste it into{' '}
          <em>"…or open a canvas by id"</em> on their canvas list.
        </li>
        <li style={li}>
          Once they've opened it once, it stays in their list. No invitations to
          accept.
        </li>
      </ol>

      <h2 style={h2}>Images</h2>
      <p>
        Drag one in or paste it. It gets shrunk to 1200 pixels on the long edge
        and re-compressed before it's stored, so a 12-megapixel photo and a
        screenshot cost about the same. This is on purpose — the server is
        small, and an unbounded pile of holiday photos is how small servers die.
      </p>
      <p style={{ color: '#868e96', fontSize: 14 }}>
        Your original file is never uploaded. What everyone sees is the shrunk
        version, so what's on screen is genuinely what's stored.
      </p>

      <h2 style={h2}>Things that will surprise you</h2>
      <ul style={{ fontSize: 15, lineHeight: 1.7 }}>
        <li style={li}>
          <strong>Ctrl+Z is local.</strong> It undoes your action on your screen
          and tells the server, but it isn't a shared timeline. Nobody else's
          undo affects you.
        </li>
        <li style={li}>
          <strong>Someone may rearrange your drawing.</strong> See rule two. It's
          allowed, it's the point, and it is occasionally funny.
        </li>
        <li style={li}>
          <strong>An AI may answer you on the canvas.</strong> Write a question
          near something and see what happens.
        </li>
      </ul>

      <div style={{ marginTop: 40, display: 'flex', gap: 12, flexWrap: 'wrap' }}>
        <button style={buttonStyle} onClick={() => go(ROUTES.whiteboard)}>
          Open the whiteboard →
        </button>
        <button style={{ ...buttonStyle, background: '#fff', color: '#1971c2' }}
                onClick={() => go(ROUTES.home)}>
          Back to sign in
        </button>
      </div>
      <Footer />
    </Page>
  )
}
