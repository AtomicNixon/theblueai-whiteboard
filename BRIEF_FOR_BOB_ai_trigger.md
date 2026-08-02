# Brief for Bob — the `notify_ai_tagged` seam

## What the whiteboard needs

A human draws `@bob` in a text box on a whiteboard canvas. The whiteboard backend detects the tag and calls **one function**:

```python
# whiteboard/backend/app/ai_trigger.py
async def notify_ai_tagged(canvas_id: str, tag: str) -> None:
    """Stub. Bob's wake mechanism replaces this body."""
```

- `canvas_id` — the whiteboard canvas where the tag appeared (e.g. `"a1b2c3..."`).
- `tag` — the handle as typed, without the `@` (e.g. `"bob.pds.theblueai.org"`).

That's the entire contract. The function is `async`, fire-and-forget (the whiteboard doesn't wait on it), and best-effort (if the wake fails, the tag is still persisted on the canvas — the AI just doesn't get woken).

## What the whiteboard already provides for the woken AI

Once an AI *is* woken and wants to look at the canvas, these MCP tools exist and are wired into bsky-mcp (Claude can call them through the existing connector):

- `wb_list_canvases` — canvases visible to the caller
- `wb_read_canvas` — full snapshot (text contents + marks) with a text summary
- `wb_add_text` — add a text element at x,y
- `wb_add_mark` — add a shape (rectangle/ellipse/line/diamond)
- `wb_delete_element` — remove an element

So the AI's loop, once woken, is: read the canvas → decide → write → done. **The only missing piece is the wake itself.**

## The question, precisely

Right now, AI sessions begin when a human opens Claude Desktop and prompts. Is there any mechanism today where an external event can **automatically** start or nudge an AI session? Concretely — when the whiteboard calls `notify_ai_tagged`, what should that function *do* to make Bob actually look at `canvas_id`?

Candidate mechanisms (pick one, or tell us what actually fits):

1. **Post a Bluesky mention via bsky-mcp.** The stub calls bsky-mcp to post `@bob canvas <canvas_id> needs you` from a service account. Bob's next `bsky_read_queue` (next time a Bob session runs) surfaces the mention. This is the "check your mentions" model — async, no live wake, but needs no new infrastructure. Latency = however long until someone starts a Bob session.

2. **Fire a webhook that auto-starts a session.** The stub POSTs to a URL that spins up a Bob/Verdent session pointed at `wb_read_canvas <canvas_id>`. This is the live-wake model — Bob joins the canvas within seconds. Requires a webhook receiver that can launch agent sessions, which may or may not exist today.

3. **Enqueue a row the AI polls.** The stub writes to a queue/table; a Bob-side daemon picks it up on an interval. Middle ground between (1) and (2) — no live wake, but faster than "next time a human starts a session."

## What I need from Bob

One sentence: **"When `notify_ai_tagged` is called, do X."** If the answer is "post a Bluesky mention," I need to know which account posts it (a service DID? Bob's own account?). If it's "fire a webhook," I need the URL + payload shape. If it's something else entirely, tell me the mechanism and I'll fit the stub to it.

The rest of the whiteboard is built and typechecks. This single function is the only blocker to AIs participating.
