# claude-slack-bot — design (v2)

**Date:** 2026-06-22
**Status:** design under revision (v1 tight-core code already on `main`; this v2 supersedes it and adds the pins/worktree/✅ model)
**Relationship:** sibling fork of `claude-tg-bot`. Shares conceptual core (`Session`, `StatusBar`, `ReplyOnce`, `sessions.json`), rewrites the transport + formatting + routing for Slack. Deliberate fork — no shared transport abstraction for a single-user tool.

> **History:** v1 (tight core) is built and committed (mrkdwn formatter, Session model, routing, simplified turn loop, StatusBar, `.cancel/.plan/.auto`, a custom pinned *index message*, restart-resume). v2 below revises the navigation model (native pins instead of the custom index), adds ✅-reaction cleanup, the worktree flow, and a merge-aware GC. Differences from the built code are called out as **[CHANGE]**.

## Goal

A single-user Slack bot wrapping Claude Code via the Agent SDK. One channel (`#claude`) is the control surface; each top-level message starts a Claude session bound to its thread. The channel doubles as a demo surface — others can watch, only the owner drives.

## Core interaction model

```
📌 Pinned panel  =  live sessions   (each pin is YOUR raw message; shows your prompt text)
│
your raw top-level message  ──────────  ← thread root + session key (thread_ts); AUTO-PINNED
   └─ 🟢 status/header   (bot's first in-thread reply — bot-owned, editable: busy, repo, branch)
   └─ bot response …      (all bot output threads under your message)
   └─ bot response …
   └─ you reply           → continues the session
✅ react on the raw message  → DONE: unpin + remove worktree + disconnect session

channel history  =  everything + where you start new sessions
```

- **A thread = a session.** Routing key is `thread_ts` (the root message's `ts`). Thread replies route to `sessions[thread_ts]`.
- **All bot output threads under your raw message** (existing `ReplyOnce`/StatusBar behavior — every send carries `thread_ts`).
- **Navigation is the native pinned-items panel, not a custom message. [CHANGE]** On session creation the bot pins your raw root message (`pins.add`). The panel becomes the always-one-tap-from-the-input live-session list. The custom `refresh_index`/`index_ts` machinery from v1 is **removed**.
- **Status lives inside the thread, not in the pin.** Bots can't edit a user's message, so the pin shows your prompt text (fine — readable "what is this"). The bot's first in-thread reply is an editable header showing 🟢 idle / 🟡 busy and, once isolated, the repo + `slack/<branch>`.

## Done = ✅ reaction

- Subscribe to `reaction_added`. When `reaction == "white_check_mark"`, `user == ALLOWED_USER_ID`, and `item.ts` is a known session key:
  1. `pins.remove` the root message
  2. `git worktree remove` the session's worktree (if any; branch kept as PR source)
  3. `disconnect()` the `ClaudeSDKClient` and drop it from `sessions` (history stays resumable via persisted `session_id`)
- A ✅ on a *reply* (not the root) carries the reply's ts, not the thread's — **ignored** in v1 (don't resolve parents).
- `reaction_removed` (un-✅) does **not** reopen the session in v1.

## Worktree flow (opt-in, Seam B — spike-proven)

**Default: sessions run in `official-repos` with no worktree** (cwd = `DEFAULT_CWD`, `acceptEdits`). Good for exploration, cross-repo work, and quick edits. Discipline for shared trees: one writer per repo at a time.

**`.worktree <repo>`** (in-thread) isolates the session into a feature worktree:
1. `git worktree add ~/Developer/blackbird/.worktrees/<repo>__<session> -b slack/<session>` off `official-repos/<repo>`
2. respawn the session's client: `disconnect()` the old one, build a new `ClaudeSDKClient(options=ClaudeAgentOptions(cwd=<worktree>, resume=<session_id>, permission_mode=<mode>))`, `connect()`, update `sess.cwd`
3. update the in-thread header to show the repo + branch

Respawn only happens **between turns** (session idle), so no in-flight turn is disrupted. If busy, defer with a "wait for the current turn" notice.

**Spike findings (verified 2026-06-22, both reproduced on a throwaway repo+worktree):**
- Resume into a *different* cwd **preserves conversation continuity** (recalled a planted token across the respawn).
- `session_id` is **unchanged** — no fork.
- New turns append to the session's **birth bucket** (`~/.claude/projects/<birth-cwd-key>/<id>.jsonl`), **not** the worktree's bucket — so no phantom jsonl / drift (the tg-bot's drift bug does not apply to plain resume-with-new-cwd).
- The agent's **tools run in the resume cwd**: `pwd` reported the worktree and a written file landed in the worktree, not the original repo.
- Consequence: a terminal `cresume` must use the birth cwd (`official-repos`) to find the transcript. Irrelevant to the bot.

This is why isolation is *opt-in at the point you decide it's feature work*, not forced on every session — matches the "chat before code" workflow without taxing small changes.

## Sustainability / cleanup

- **Disk reality:** worktrees share `.git/objects` (history not duplicated) — cheap. The real cost is **`node_modules` / build artifacts per worktree** (hundreds of MB each, not shared). Worktree sprawl hurts via these, not git. Mitigation = aggressive GC; optional future: share/symlink `node_modules`.
- **Pin cap:** Slack allows 100 pinned items per channel. The ✅-to-unpin discipline keeps the panel (and worktree count) bounded — that's the forcing function.
- **Merge-aware GC — `/gc` (slash command):** scans `slack/*` branches; for each whose PR is merged (`gh pr list --state merged` and/or `git branch --merged origin/main`), `git worktree remove` + `git branch -d`. Cleanup tracks **merge reality**, not memory. Manual in v1; a scheduled poller is a deferred option. The GC `log()`s what it reaps and skips (no silent truncation).

## Bumping older threads

- **Slack has no API to reorder/move a channel message.** Threads don't float on reply (without broadcast).
- **Pins solve discovery:** the pinned panel is one tap from the input regardless of scroll position, so "buried in scroll" stops mattering — you navigate by panel, not by scrolling.
- **Recency ordering within the panel — LIVE TEST REQUIRED:** *if* pinned items order by pin-time, then "bump on activity" = `pins.remove` + `pins.add` to float a session to the top. *If* they order by message-time, re-pinning won't move them (panel stays a stable findable list). Confirm pin ordering on first live run before building bump-on-activity.
- **Escape hatch:** a thread reply with `reply_broadcast=true` drops a copy at the channel bottom — opt-in "ping me in-channel when this turn finishes."

## Command surface

| Trigger | Where | Effect |
|---|---|---|
| top-level message | channel | create session (auto-named, auto-pinned), prompt = message; bot replies in-thread |
| thread reply | in-thread | continue that session |
| `.cancel` | in-thread | interrupt current turn |
| `.plan` / `.auto` | in-thread | switch permission mode |
| `.worktree <repo>` | in-thread | isolate session into a feature worktree (resume-into-worktree) |
| ✅ on root message | reaction | done: unpin + remove worktree + disconnect |
| `/gc` | slash | reap worktrees/branches whose PRs are merged |

Per-session controls are in-thread `.`-prefixed messages (slash commands lack `thread_ts`). Only thread-agnostic actions (`/gc`) are real slash commands.

## Architecture

- **Transport:** `slack_bolt` async + `AsyncSocketModeHandler` (no public URL). Needs `SLACK_BOT_TOKEN` (`xoxb-`) + `SLACK_APP_TOKEN` (`xapp-`, `connections:write`). Requires `aiohttp` (no `[async]` extra in current `slack_bolt`).
- **Concurrency:** the `message` handler dispatches each turn via `asyncio.create_task` and returns immediately, so a long turn never blocks other sessions. Same-session ordering stays safe: the `busy` check-and-set in `drive_session` is atomic under asyncio (no `await` between them).
- **Allowlist:** acts only on `ALLOWED_USER_ID` in `CLAUDE_CHANNEL_ID`. Load-bearing — the bot runs Claude Code with full host access.
- **Persistence:** `sessions.json` holds `{name, cwd, mode, model, session_id, channel_id, thread_ts, worktree?}` per session (only those with a captured `session_id`). On startup, `restore_sessions()` rebuilds each by resuming its `session_id` (with its persisted cwd — including a worktree cwd). **[CHANGE]** add `worktree` path field; drop `index_ts`.

## Reused / rewritten / cut

- **Reused conceptually:** `Session`, `StatusBar` (the in-thread editable header), `ReplyOnce`, `sessions.json`.
- **Rewritten for Slack:** transport (Socket Mode), formatter (`to_mrkdwn` — fenced code stashed first, then headers→bold, links→`<url|text>`, `**`→`*`; unit-tested), routing on `thread_ts`/`channel_id`.
- **Cut from this milestone (future):** jsonl terminal-mirror / `/respawn` / `/sync`; `/adopt`, `/digest`, `/todo`, `/move`, `/btw`, `/cc`; chief orchestration; rename; PR-create-on-✅ (could fold into the ✅ hook later); scheduled GC; `node_modules` sharing; `reply_broadcast` auto-ping. Visual-block `.txt` hack intentionally dropped (Slack renders monospace fine).

## Config / scopes

```
SLACK_BOT_TOKEN=xoxb-...
SLACK_APP_TOKEN=xapp-...
ALLOWED_USER_ID=U...
CLAUDE_CHANNEL_ID=C...
DEFAULT_CWD=/Users/noahchun/Developer/blackbird/official-repos
```
**Bot scopes:** `chat:write`, `commands`, `channels:history`, `channels:read`, `pins:write`, `reactions:read`, `groups:history`, `groups:read`. **[CHANGE]** add `reactions:read`.
**Events:** `message.channels`, `message.groups`, `app_mention`, `reaction_added` (and optionally `reaction_removed`). **[CHANGE]** add reaction events.
**Socket Mode** on; app-level token with `connections:write`.

## Open questions to resolve on first live run

1. **Pin ordering** — pin-time or message-time? Decides whether bump-on-activity (unpin+repin) is buildable.
2. **`reaction_added` payload** — confirm `item.ts` equals the root `thread_ts` for a root-message reaction.
3. **Pin/unpin channel noise** — does the "pinned a message" system line bother the workspace enough to switch to `bookmarks` instead of pins?
4. **`.worktree` while `<repo>` is dirty / branch exists** — fallback path (the tg-bot retries `worktree add` without `-b` when the branch already exists).

## Hosting & file map

Same Mac; launchd plist (ported). `bot.py` single file. `requirements.txt`: `slack_bolt`, `aiohttp`, `claude-agent-sdk`, `python-dotenv`. `manifest.yml`, `restart.sh`, `claude-slack-bot.plist.template`, `tests/test_mrkdwn.py`, `SETUP.md`, `CLAUDE.md`.
