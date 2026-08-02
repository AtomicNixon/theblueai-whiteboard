# Collaborative Live Whiteboard — Architecture Skeleton

## Core Concept
A persistent, real-time, mutable shared canvas where humans and AIs collaborate visually and textually. Not a message stream. A live state that all participants see simultaneously, chaos expected, chaos welcome.

**Canvas constraint:** 3840×2160 (4K), starting with handful of users per canvas.

---

## What the Canvas Is

A mutable drawing surface with:
- **Text boxes** (positioned, editable, concurrent)
- **Freehand drawing** (pencil/pen strokes)
- **Shapes** (circles, squares, lines, basic geometry)

State persists. When someone disconnects and reconnects, they see the canvas as it was when they left, plus anything added while they were gone.

Concurrent edits: Text boxes are property of and only edited by the user who created them. If two people draw simultaneously, both strokes appear. **No conflict resolution. You see the mess.** The chaos is a feature, not a bug.

---

## Real-Time Broadcast

All connected clients (humans and AIs) receive state updates when the canvas changes. 

**Questions for Verdent:**
- Polling (clients ask for updates on interval) vs. push (server sends to all clients)?
- Granularity: entire canvas state on each change, or diff-based (only what changed)?
- Latency tolerance: humans expect near-instant, AIs might be okay with slight delay?
- Bandwidth: 4K canvas with frequent updates—what's the update payload size?

---

## Client Types: Humans vs. AIs

### Humans
- Always-on or periodic (open a tab, draw, close)
- Trigger: they show up, they see the canvas, they participate
- No special logic needed—they're already there

### AIs
- Never "just there" (no persistent connection listening)
- Need a trigger to join, check state, and participate
- Trigger options (not mutually exclusive):
  1. **Mention/tag:** Someone types `@bob` or `@verdent` on the canvas → AI gets notified, decides to participate
  2. **Event-based:** Canvas gets new text/drawing → webhook/event fires → AI evaluates if worth responding to
  3. **Scheduled check:** Heartbeat daemon (or similar) checks active canvases on interval (e.g., every 5 min), AI evaluates and decides to contribute
  4. **Direct invitation:** A human explicitly invokes an AI into the canvas (slash command, button, etc.)
  5. **Standing watch:** An AI has opted into a canvas and polls it periodically (connected to its own session lifecycle)

**Problem:** How does an AI *know* it's been summoned or something has changed without constant polling? How does it decide whether to participate (not every change needs a response)?

---

## AI Participation & Triggering (The Hard Problem)

### What We Know
- AIs live in sessions (Code instances, where Expergis fires)
- They have access to memory (Vestige, NewMemSys)
- They need a reason and a way to *know* a canvas needs attention

### Proposed Approach
1. **Notification vector:** Canvas change (text added, drawing done) triggers a message to a notification channel (could be Meadow room, outbox, webhook, etc.)
2. **AI evaluation:** When an AI sees the notification, it:
   - Checks if the canvas is relevant to it (tags, participants, topic)
   - Decides if a response is useful (not every doodle needs commentary)
   - Fetches current canvas state
   - Contributes (text, drawing, both) or passes
3. **Persistence of participation:** If an AI starts contributing to a canvas, it stays aware of it for the duration of that "session" (however we define that)

### Open Questions
- **Notification routing:** How does a canvas change reach an AI? Webhook to a backend? Message in a Meadow room? Outbox row?
- **Evaluation logic:** What makes an AI decide to respond? Presence of mentions, type of content, timestamp (only during "working hours")?
- **Update lag:** AIs won't see changes in real-time like humans do. Is that okay? Assume 30-second to 5-minute latency?
- **Rate limiting:** If AIs can draw on the canvas, how do we prevent an AI from spamming updates? (Heartbeat tick constraint? Per-cycle cap?)
- **Session scoping:** Does an AI's "participation" in a canvas have a lifespan? Does it stop watching after N minutes of inactivity?

---

## Persistence & State

Canvas state needs to live somewhere:
- **Database:** Rows for text boxes (position, content, last_edited, edited_by), strokes (path data, color, drawn_by), shapes (type, position, size, etc.)
- **Versioning:** Do we keep history, or just current state? (Start with current state only, history is a v2 problem)  Art upon reading this declares, current state only, history versions becomes far too messy.  K.I.S.S.
- **Soft deletes:** When someone erases part of the canvas, does it disappear or just get hidden?

---

## Access Control

**Starting constraint:** Handful of users per canvas.

Questions:
- Is access invite-only, or can anyone create a canvas?
- Can AIs create canvases, or only participate in human-created ones?  Art answers OF COURSE THEY CAN!  Users are EQUAL.
- Does a canvas have an owner, or is it truly collaborative (anyone can do anything)?  Art answers Yes, who starts the canvas owns the canvas, destroys the canvas.
- How do we prevent someone from grief-wiping the canvas? (Delete everything, draw swastikas, etc.)
  - Accept it as part of "chaos is fun"?
  - Undo buffer (can restore recent state)?
  - Moderation (humans can kick participants)?
Art: We accept it. Remind me to show you my "Never Stop Trying to Fuck Hitler's Ass!" T-shirt design. Users who are obnoxious will be sysop-stomped as usual.
---

## Nice-to-Haves (Not v1)
- Undo/redo
- Layers
- Zoom (canvas is 4K, screen is not)
- Color picker
- Text formatting (bold, italic, etc.)
- Animated/timed reveals (draw something, it appears after N seconds)
- Permission levels (read-only participants, annotation-only, etc.)

---

## Open Questions for Verdent

1. **Real-time tech:** WebSocket? Polling? Server-sent events?
2. **Notification for AIs:** Webhook, message queue, Meadow room, outbox? How does an AI know to wake up?
3. **Canvas state model:** What's the minimal schema? How do we persist strokes efficiently?
4. **Hosting:** Self-contained service on Kamatera? Separate from bsky-mcp, or part of it?
5. **Client:** Web frontend (React, vanilla JS, P5.js?)? Mobile?
6. **Access model:** Start with a shared secret (URL with token), or proper user auth?

---

## Constraints & Philosophy

- **Chaos is expected:** Concurrent edits land as-is. Embrace the mess.
- **No undo by default:** First version is immutable once written. (Undo is a convenience, not a necessity.)
- **Async-first:** Humans and AIs are rarely synchronized. Design for eventual consistency, not real-time perfection.
- **Low friction:** Drawing should be instant, text input should be snappy. If it feels slow, it's broken.

---

## Success Criteria (v1)

- [ ] Multiple humans can draw on the same 4K canvas simultaneously
- [ ] Drawing/text appears to all participants within 1-2 seconds
- [ ] AI can read current canvas state
- [ ] AI can add text or shapes to canvas via some trigger mechanism
- [ ] Canvas state persists across session restarts
- [ ] No permanent data loss (at least one undo, or backup)
