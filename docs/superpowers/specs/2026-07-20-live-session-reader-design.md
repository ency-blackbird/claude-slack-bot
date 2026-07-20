# claude-slack-bot — live session reader (desync fix)

**Date:** 2026-07-20
**Status:** design approved (approach A); owner delegated spec-content review to Claude, cleared to implement after self-review.
**Scope:** core turn-loop rewrite in `bot.py`. Comprehensive fix for cross-conversation desync + unprompted (monitoring/CI-watch) output. Not a bandaid.

## Problem

Symptom (confirmed by audit of ~24 live sessions + `bot.log` + a thread scan): skunk "generates a response but doesn't actively send it to Slack until poked." A reply lands ~instantly after a *new* user message but its content answers an *earlier* one; long/background work goes silent then dumps on the next message; a restart poisons the first turn of every resumed thread.

**Root cause (verified in the SDK):** the harness only consumes the message stream *during a turn*. `drive_session` does `query()` → `receive_response()`, and `receive_response()` returns the instant it yields a `ResultMessage` (SDK `client.py:571`). After that, **nothing is listening.**

The SDK exposes **one continuous message stream per client** (`self._message_receive`, an anyio memory stream; `_internal/query.py:121`). The reader task forwards every message onto it and only sends the `{"type":"end"}` sentinel in its `finally` — i.e. **once, at subprocess exit** (`_internal/query.py:365-372`), never per-turn. `result` messages are forwarded inline and do **not** close the stream (`_internal/query.py:297-322`). So anything the agent emits after a turn's `ResultMessage` — a monitor update, CI-watch result, backgrounded work — sits **buffered with no consumer** until the next `query()`/`receive_response()` (the user's next message) drains it. That is the desync, the poke-to-flush, and the one-behind, all one bug.

The Friday `DANGLING-SKIP` guard (`bot.py` ~537-564) is a bandaid on this and has fired 0 times (its 2s window misses the real ~4.6s latency). It is **removed** by this design.

## Goals / non-goals

**Goals**
- Output flows into the correct thread the instant the agent produces it — prompted replies *and* unprompted updates (the CI-watch use case that motivated monitoring).
- Cross-conversation desync eliminated *structurally*, not heuristically.
- Restart-resume is clean: a resumed session streams correctly on its first message, no poke.
- The turn model becomes simpler to reason about: one rule replaces the leaky "a turn is the only time we listen" abstraction + its patches.

**Non-goals (explicit future seams, not built now)**
- **Mid-turn steering** — feeding a new message into a *running* turn instead of queuing it. This is the "parallel within a thread" axis; without it, a thread that is actively watching CI stays busy and new messages queue until the watch turn ends. Known limitation, called out to the owner.
- **External / out-of-process monitor injection** (a cron or separate process pushing into a thread). No such monitor exists today (verified: no cron, no LaunchAgent, no HTTP server, no monitor code). Leave a clean seam; don't build.
- Cross-thread awareness; lazy-spawn / idle-reap of subprocesses. Separate efforts.

## Architecture: one persistent reader per session

Replace "turn-scoped reads" with **a single long-lived reader task per session that is the sole consumer of the client's message stream.** `query()` only injects the prompt; the reader delivers everything the session emits, whenever it emits it, to that session's thread.

```
  your message ──► drive_session ──► (busy? queue : _issue_prompt) ──► client.query(text)
                                                                            │
   Claude Code subprocess ── stream (one per client, spans all turns) ──────┤
                                                                            ▼
                                                    _reader_loop(sess)  [long-lived task]
                                                      • AssistantMessage.text  → say_threaded(thread_ts)
                                                      • ToolUseBlock           → StatusBar.update (if a prompt is in flight)
                                                      • ResultMessage          → end in-flight prompt: clear busy, stop StatusBar, drain queue
                                                      • unprompted text (busy False) → say_threaded anyway  ← monitoring/CI
                                                    stream ends (subprocess exit / error) → respawn client + reader
```

**Why this is comprehensive:** one reader ↔ one `thread_ts`, 1:1 for the client's life. Output physically cannot land in another conversation. There is no second consumer and no re-entered `receive_response()`, so there is no buffered tail to leak — the dangling-tail class of bug ceases to exist.

## Components

### `Session` (dataclass) — additions
- `reader_task: asyncio.Task | None` — the long-lived reader.
- `status: StatusBar | None` — the StatusBar for the in-flight prompt (None when idle / for unprompted output).
- `busy` keeps its meaning but is now **owned by the reader**: `True` = a user prompt is in flight awaiting its `ResultMessage`.
- `pending_prompts` unchanged (FIFO of user prompts received while busy).

### `_reader_loop(sess)` — new, the heart of the design
```
try:
    async for msg in sess.client.receive_messages():
        await capture_session_id(msg, sess)
        if AssistantMessage:
            for block in content:
                TextBlock  → if sess.status: stop it (first real text); say_threaded(block.text)
                ToolUseBlock → if sess.status: sess.status.update(friendly_verb(...))
        elif ResultMessage:
            await _end_turn(sess)          # stop StatusBar, busy=False, drain queue
except (stream error / EOF):
    await _respawn(sess)                   # subprocess died — reconnect + restart reader
```
- Streams **all** text regardless of `busy` — that is what makes unprompted monitor output appear live.
- The blank-placeholder / 2s guard is **gone** (not needed; single ordered consumer).

### `_issue_prompt(sess, text)` — new helper (used by drive_session AND the queue drain)
```
sess.busy = True                     # set with NO await before it (atomicity preserved)
await sess.client.query(text)        # query FIRST, then StatusBar — see note
sess.status = StatusBar(...); await sess.status.start()
sess.turn_last_activity = monotonic()  # watchdog baseline
```
**Ordering note:** `busy=True` then `query()` with no `await` between them keeps the double-issue guard. `query()` only writes the prompt to the transport (fast). StatusBar starts *after* the query is sent, so the window in which a stray `ResultMessage` could arrive before our turn exists is effectively nil. (Single prompt in flight + FIFO already guarantees the next `ResultMessage` is ours; this ordering just closes the mid-`_issue_prompt` gap.)

### `drive_session(sess, text)` — shrinks
```
if sess.busy:
    sess.pending_prompts.append(text); post "📥 queued (N)"; return
await _issue_prompt(sess, text)
```
No reading here anymore — the reader owns output.

### `_end_turn(sess)` — new, called by the reader on `ResultMessage`
```
if sess.status: await sess.status.stop(); sess.status = None
sess.busy = False
if sess.pending_prompts:
    nxt = sess.pending_prompts.popleft()
    await _issue_prompt(sess, nxt)   # no await between busy=False and this → atomic drain
```

## Concurrency & correctness

- **Atomic busy handoff (preserved invariant).** `drive_session`'s `if sess.busy … else _issue_prompt` has no `await` between the check and `busy=True` (inside `_issue_prompt`, set before its first `await`). `_end_turn` sets `busy=False` then drains with no intervening `await`. So a `_route` task and the reader task can never both issue a prompt. This is the same guarantee the current code documents, kept intact.
- **One prompt in flight at a time** → the next `ResultMessage` after a `query()` is unambiguously that prompt's. FIFO order preserved.
- **Unprompted output while idle** (`busy False`): streamed to the thread; no StatusBar; a stray `ResultMessage` while idle is a no-op for `busy`.

## Lifecycle

- **Start:** spawn `reader_task` immediately after `client.connect()` — in both session-create (`_route`) and `restore_sessions`. `asyncio.create_task(_reader_loop(sess))`.
- **Close (✅ reaction):** cancel `reader_task` (await its cancellation), then `client.disconnect()`.
- **Restore:** unchanged resume logic + start reader. First message streams correctly; no poke. (Fixes the "must restart every thread" complaint.)

## Error handling

- **Stream EOF / subprocess exit** (`receive_messages()` returns): log, then `_respawn(sess)` — `disconnect()` old client, `make_client(resume=session_id, …)`, `connect()`, restart reader. Backoff + a max-consecutive-respawn cap (e.g. 3 within 60s → give up, post "⚠️ session died, send a message to restart it") to avoid a crash loop.
- **Stream error** (`{"type":"error"}` → `receive_messages()` raises, `_internal/query.py:851`): same path as EOF (respawn), after posting a one-line error to the thread.
- **Abnormal end — a turn that never yields a `ResultMessage`:** `busy` would stay `True` and wedge the queue. Safeguard: an **idle watchdog**. `sess.turn_last_activity` is set when a prompt is issued and refreshed by the reader on *every* streamed message. A periodic check (or a per-turn timer) forces `_end_turn` when `busy` and `now - turn_last_activity > TURN_IDLE_CAP` (default 10 min, tunable), logging `WARN turn watchdog fired`. Because activity resets the timer, a long CI watch that emits periodically never trips it — only a genuinely dead/silent turn does.
- **`say_threaded` failures** already swallow per-chunk; a Slack outage cannot kill the reader.

## Interactions with existing features
- **Approval gate (`can_use_tool`)** — unchanged; it runs inside the SDK during a turn and still posts buttons to the thread. (Its own blocking-on-await is a separate concern, not touched here.)
- **`.cancel` / `.plan` / `.auto`** — `.cancel` calls `client.interrupt()`; the reader then sees the turn's terminal `ResultMessage` and runs `_end_turn` normally. `.plan/.auto` unchanged.
- **Pin / ✅ teardown / `/list`** — unchanged except close() also cancels the reader.

## Testing
1. **Isolation test (before any restart)** — mock async stream (asyncio.Queue-backed, mirroring `_message_receive`) driving `_reader_loop`/`_end_turn` with fakes for `say_threaded`/StatusBar. Assert:
   - prompted turn: text streamed in order, StatusBar stopped on first text, `busy` cleared on `ResultMessage`, queue drained;
   - **unprompted** text (arrives with `busy False`): still posted to the thread;
   - two queued prompts drain in FIFO order, one at a time;
   - stream EOF triggers respawn exactly once; error message posts;
   - watchdog forces `_end_turn` after the idle cap with no `ResultMessage`.
2. **Existing formatter tests** (`tests/test_mrkdwn.py`) still pass (import smoke).
3. **Manual, post-restart:** (a) fire two quick messages back-to-back — each reply matches its own message; (b) kick off a CI watch, confirm updates stream in with no poke; (c) restart the bot and send one message to a resumed thread — clean first reply.

## Rollout & revert
- Live bot (PID under launchd). Implement → isolation test green → `./restart.sh 15` (owner-granted standing agency) → verify per §Testing.3.
- Revert = `git checkout bot.py` (change is contained to `bot.py`) + restart. The reader is additive; reverting restores the current turn loop.

## Future seams (noted, not built)
- **Mid-turn steering** via SDK streaming input → `_route` sends into the live turn instead of queuing; unblocks chat-while-watching.
- **External monitor injection** endpoint (if out-of-process monitors are ever built).
- **Lazy-spawn + idle-reap** of subprocesses (resource hygiene: 24 resident processes today).
