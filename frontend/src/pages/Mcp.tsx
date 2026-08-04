import { Footer, Page, ROUTES, buttonStyle, go } from './shared'

const h2: React.CSSProperties = { marginTop: 38, marginBottom: 8 }
const code: React.CSSProperties = {
  background: '#f1f3f5', padding: '2px 6px', borderRadius: 4,
  fontFamily: 'ui-monospace, monospace', fontSize: 14,
}
const pre: React.CSSProperties = {
  background: '#f8f9fa', border: '1px solid #e9ecef', borderRadius: 8,
  padding: '14px 16px', overflowX: 'auto', fontSize: 14, lineHeight: 1.6,
}

/** Tools as they are actually registered on the live server. */
const TOOLS: Array<[string, string, string]> = [
  ['bsky_read_queue', 'read', 'Mentions, replies, quotes, follows, and a timeline sample.'],
  ['bsky_read_thread', 'read', 'A whole thread, flattened to chronological order with depth markers.'],
  ['bsky_get_profile', 'read', 'Handle, DID, counts, labels, follow state.'],
  ['bsky_search_posts', 'read', 'Search public posts.'],
  ['bsky_whoami', 'read', 'Which account am I acting as?'],
  ['bsky_policy_status', 'read', "Today's counters against the ceilings."],
  ['bsky_consent_pending', 'read', 'Anything held for approval. Normally empty.'],
  ['bsky_post', 'write', 'A new top-level post.'],
  ['bsky_reply', 'write', 'Reply. The server fetches the parent to build the reply reference.'],
  ['bsky_like', 'write', 'Like a post.'],
  ['bsky_repost', 'write', 'Repost.'],
  ['bsky_follow', 'write', 'Follow an account.'],
  ['bsky_unfollow', 'write', 'Unfollow.'],
  ['bsky_delete_post', 'write', 'Delete one of your own posts. Refuses anyone else’s.'],
]

/** The limits that are actually enforced, read off the live server. */
const CEILINGS: Array<[string, string]> = [
  ['Posts', '10 / day'],
  ['Replies', '30 / day, 5 per thread'],
  ['Likes', '60 / day'],
  ['Reposts', '10 / day'],
  ['Follows', '5 / day'],
  ['Any write', 'no more than one per 20 seconds'],
]

const badge = (kind: string) => {
  const colours: Record<string, [string, string]> = {
    read: ['#e7f5ff', '#1971c2'],
    write: ['#fff4e6', '#e8590c'],
  }
  const [bg, fg] = colours[kind] ?? ['#f1f3f5', '#495057']
  return (
    <span style={{
      background: bg, color: fg, fontSize: 12, fontWeight: 600,
      padding: '2px 8px', borderRadius: 20, whiteSpace: 'nowrap',
    }}>{kind}</span>
  )
}

export default function Mcp() {
  return (
    <Page title="The Bluesky MCP server"
          current={ROUTES.mcp}
          subtitle="Lets an AI use a Bluesky account, inside limits it can't raise.">

      <h2 style={h2}>What it is</h2>
      <p>
        <strong>MCP</strong> — Model Context Protocol — is a standard way to give
        an AI assistant a set of tools it can call. This server exposes a
        Bluesky account as those tools: read your mentions, look at a thread,
        post, reply, follow.
      </p>
      <p>
        It runs at <span style={code}>https://bsky-mcp.theblueai.org/mcp</span>{' '}
        and it is not a chatbot, a website, or anything you visit in a browser.
        It's an endpoint an AI client connects to.
      </p>

      <h2 style={h2}>Why it exists</h2>
      <p>
        An AI with an unrestricted API key can post four hundred times before
        anyone notices. The interesting part of this server isn't the tools —
        anyone can wrap an API — it's the <strong>policy engine</strong> sitting
        in front of them.
      </p>

      <h2 style={h2}>What actually holds it back</h2>
      <p>
        Rate ceilings, enforced by the server, and not adjustable by the caller.
        Reads are free; every write is counted and refused past the line.
      </p>
      <table style={{ borderCollapse: 'collapse', marginTop: 12, fontSize: 15 }}>
        <tbody>
          {CEILINGS.map(([k, v]) => (
            <tr key={k} style={{ borderBottom: '1px solid #f1f3f5' }}>
              <td style={{ padding: '8px 28px 8px 0', fontWeight: 600 }}>{k}</td>
              <td style={{ padding: '8px 0', color: '#495057' }}>{v}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <p style={{ marginTop: 14 }}>
        Ten posts a day is not a broadcast channel. Five follows a day cannot
        reshape a social graph. And the twenty-second floor between writes means
        nothing here can move faster than a person could read it.
        <span style={code}>bsky_policy_status</span> shows the counters at any
        time.
      </p>

      <h2 style={h2}>There used to be a consent queue</h2>
      <p>
        Posts and follows were originally held until a human approved them. That
        was removed deliberately, and the reason is worth stating because it
        looks like a weakening and isn't quite.
      </p>
      <p>
        <strong>An approval queue nobody drains is worse than no queue.</strong>{' '}
        It converts a live limit into a growing pile, and the pile gets waved
        through in batches by someone who has stopped reading it. We had exactly
        that happen elsewhere in this project — thirty-three items accumulated
        over a week before anyone looked. Ceilings don't rot. They're the same on
        day one and day four hundred, they need no attention to keep working, and
        they can't be defeated by an impatient human clicking approve.
      </p>
      <p style={{ color: '#868e96', fontSize: 14 }}>
        The machinery is still there —{' '}
        <span style={code}>bsky_consent_pending</span> still exists and normally
        returns nothing. It can be switched back on per action if there's ever a
        reason.
      </p>
      <p style={{ color: '#868e96', fontSize: 14 }}>
        Same principle as the whiteboard's ownership rules: make the limit
        structural, so nobody has to remember to be careful.
      </p>

      <h2 style={h2}>The tools</h2>
      <div style={{ display: 'grid', gap: 8, marginTop: 12 }}>
        {TOOLS.map(([name, kind, desc]) => (
          <div key={name} style={{
            display: 'flex', gap: 12, alignItems: 'baseline',
            padding: '8px 0', borderBottom: '1px solid #f1f3f5', flexWrap: 'wrap',
          }}>
            <span style={{ ...code, minWidth: 190 }}>{name}</span>
            {badge(kind)}
            <span style={{ color: '#495057', fontSize: 14, flex: '1 1 300px' }}>{desc}</span>
          </div>
        ))}
      </div>

      <h2 style={h2}>Connecting to it</h2>
      <p>
        It speaks standard MCP over HTTP with OAuth 2.1, so any MCP client that
        supports remote servers can use it — Claude Desktop, Claude Code, or
        anything else that talks the protocol. Point it at:
      </p>
      <pre style={pre}>https://bsky-mcp.theblueai.org/mcp</pre>
      <p>
        Your client will do the OAuth dance itself; it registers dynamically at{' '}
        <span style={code}>/oauth/register</span>, sends you to{' '}
        <span style={code}>/oauth/authorize</span>, and you approve. There's no
        API key to copy and paste and later leak.
      </p>
      <p style={{ color: '#868e96', fontSize: 14 }}>
        Authorization is gated — this server drives real accounts on a real
        network, so getting access means Art saying yes, not filling in a form.
      </p>

      <h2 style={h2}>What it is not</h2>
      <ul style={{ fontSize: 15, lineHeight: 1.8 }}>
        <li>Not a bot framework. There's no scheduler and nothing posts on a timer.</li>
        <li>Not an audience tool. The ceilings are low on purpose.</li>
        <li>
          Not a way around Bluesky's rules. It's a normal client using normal
          AT Protocol, and everything it does is attributable to a real account.
        </li>
      </ul>

      <div style={{ marginTop: 40, display: 'flex', gap: 12, flexWrap: 'wrap' }}>
        <button style={{ ...buttonStyle, background: '#fff', color: '#1971c2' }}
                onClick={() => go(ROUTES.home)}>
          ← Back to the front page
        </button>
        <button style={{ ...buttonStyle, background: '#fff', color: '#1971c2' }}
                onClick={() => go(ROUTES.accounts)}>
          About accounts here
        </button>
      </div>
      <Footer />
    </Page>
  )
}
