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
  ['bsky_policy_status', 'read', "Today's counters against the ceilings, and the pending queue."],
  ['bsky_consent_pending', 'read', 'Actions waiting for a human to approve.'],
  ['bsky_like', 'direct', 'Like a post. No approval needed.'],
  ['bsky_repost', 'direct', 'Repost. No approval needed.'],
  ['bsky_reply', 'direct', 'Reply. The server fetches the parent to build the reply reference.'],
  ['bsky_post', 'queued', 'A new top-level post. Waits for approval by default.'],
  ['bsky_follow', 'queued', 'Follow someone. Waits for approval.'],
  ['bsky_unfollow', 'queued', 'Unfollow. Waits for approval.'],
  ['bsky_delete_post', 'queued', "Delete one of your own posts. Waits for approval."],
]

const badge = (kind: string) => {
  const colours: Record<string, [string, string]> = {
    read: ['#e7f5ff', '#1971c2'],
    direct: ['#ebfbee', '#2f9e44'],
    queued: ['#fff4e6', '#e8590c'],
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
          subtitle="Lets an AI use a Bluesky account — with a leash on it.">

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
      <p>Every action is one of three kinds, and the kind is decided by the server, not the caller:</p>
      <ul style={{ fontSize: 15, lineHeight: 1.8 }}>
        <li>{badge('read')} — happens immediately. Reading costs nothing and harms nobody.</li>
        <li>{badge('direct')} — happens immediately, but it's a write. Likes, reposts, replies.</li>
        <li>
          {badge('queued')} — <strong>does not happen</strong> until a human approves it.
          The tool returns "queued", and it sits in a list until someone says yes.
        </li>
      </ul>
      <p>
        New posts, follows, unfollows and deletions are queued by default. So an
        AI can carry on a conversation on its own, and cannot start broadcasting
        or reshape who it follows without a person agreeing to it. There are also
        daily ceilings, visible any time via{' '}
        <span style={code}>bsky_policy_status</span>.
      </p>
      <p style={{ color: '#868e96', fontSize: 14 }}>
        This is the same idea as the whiteboard's ownership rules: make the
        limits structural, so nobody has to remember to be careful.
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
