# claude-slack-bot — v1 design (tight core)

**Date:** 2026-06-22
**Status:** approved design, pre-implementation
**Relationship:** sibling fork of `claude-tg-bot`. Shares conceptual core (`Session`, `StatusBar`, `ReplyOnce`, `sessions.json`), rewrites the transport + formatting + routing for Slack. Not a shared codebase — deliberate fork to avoid a leaky transport abstraction for a single-user tool.

## Goal

A Slack bot that wraps Claude Code via the Agent SDK, giving a phone-friendly interface to multiple parallel Claude conversations. Single-user (per install), long-lived Python process on macOS. Lives in one dedicated Slack channel (`#claude`) that doubles as a demo surface — others can watch, only the owner can drive.

## Core model: channel + threads-as-sessions

```
#claude  (one channel)
│
├─ 📌 pinned: SESSION INDEX  →  🟢 add-tests · 🟡 refactor-auth · ⚪ scratch   (clickable permalinks)
│
├─ ▸ root msg "🟢 add-tests"          ← thread_ts = session key
│     └─ user: "add a test for X"
│     └─ bot: [StatusBar] then reply (in-thread)
│
└─ ▸ root msg "🟡 refactor-auth"
      └─ user: "what's left?"
      └─ bot: reply (in-thread)
```

- **A thread = a session.** Routing key is `thread_ts` (analog of Telegram's `message_thread_id`).
- **Talk to a session** = reply in its thread.
- **Pinned session index** = the `/list` analog; always visible, one tap into any session via `chat.getPermalink`.

## Session creation, naming, cwd

- **Creation gesture:** a plain top-level (non-threaded, non-command) message in `#claude`. The bot creates a session rooted at that message's `ts` (becomes `thread_ts`), treats the message text as the first prompt, and replies in-thread.
- **Naming:** auto-generated from a slug of the first few words of the creating message. Shown in the pinned index. (No rename in v1.)
- **cwd:** **every session uses `DEFAULT_CWD = ~/Developer/blackbird/official-repos`.** No per-session pinning. The cwd is a starting context, not a boundary — Claude Code has full tool access and works across all repos underneath the parent, which directly serves the user's parallel + cross-repo workflow. Per-repo `CLAUDE.md`/`CLAUDE.local.md` is picked up as Claude works inside a given repo.

## Command surface (v1)

| Trigger | Where | Effect |
|---|---|---|
| top-level message | channel | create new session, first prompt = message text |
| thread reply | in-thread | route message to that thread's session |
| `.cancel` | in-thread | interrupt the session's current turn (`client.interrupt()`) |
| `.plan` | in-thread | switch session to plan permission mode |
| `.auto` | in-thread | switch session to acceptEdits permission mode |
| `/list` | slash (global) | repost / refresh the pinned session index |

**Why the slash-vs-dot split:** Slack native slash commands do **not** carry `thread_ts` in their payload, so a slash command typed in a thread can't tell the bot which session it targets. Per-session controls therefore ride in as ordinary in-thread messages with a `.` prefix — the message event *does* include `thread_ts`. Global, thread-agnostic actions (`/list`) stay as real slash commands.

## Architecture

- **Transport:** `slack_bolt` in **Socket Mode** (`SLACK_APP_TOKEN`). No public URL / webhook — the direct analog of Telegram long-polling, ideal for a Mac at home. Interactive components (if added later) also work over Socket Mode with no extra infra.
- **Concurrency:** Bolt async app; handlers must not block other sessions during a long Claude turn (analog of PTB `concurrent_updates(True)`).
- **Routing:** `(channel_id, thread_ts) → Session`. A message with no `thread_ts` (or whose `thread_ts == its own ts`) in `#claude` is a creation event.
- **Allowlist:** the bot obeys only `ALLOWED_USER_ID`. Messages from any other user are ignored (logged, not acted on) — they can watch but not drive. Load-bearing because the bot runs Claude Code with full access to the host.

## Reused conceptual units (ported, not shared code)

- **`Session`** dataclass — fields: `name`, `cwd`, `client` (`ClaudeSDKClient`), `mode`, `model`, `busy`, `session_id`, `thread_ts`, `channel_id`, `pending_prompts` (FIFO for messages received while busy). Drops Telegram-only fields (`topic_id`, mirror/marker fields, chief fields).
- **`StatusBar`** — a message posted at turn start (`chat.postMessage`), updated with `📖 reading X` / `🛠️ running Y` via `chat.update` as tool calls fire, deleted (`chat.delete`) when real assistant text begins. Debounced ≥1s/edit for Slack rate limits.
- **`ReplyOnce`** — first bot message of a turn anchors the thread; subsequent messages in the same turn post in-thread without re-quoting.
- **`sessions.json`** — same JSON-file persistence. Persists `session_id`, `cwd`, `mode`, `model`, `thread_ts`, `channel_id`, `name` per session so the bot resumes after restart. Conversation history stays owned by Claude Code in `~/.claude/projects/<cwd-hash>/<session_id>.jsonl`.

## Rewritten for Slack

- **Formatter: Telegram MDV2 → Slack `mrkdwn`.** `*bold*` (single asterisk), `_italic_`, `~strike~`, `` `code` ``, ```` ```block``` ````, `>quote`, `<url|text>` links. No headers (render as bold). Plain-text fallback per chunk on failure. This is the largest single chunk of work.
- **Message chunking:** split long output at ~3500 chars (Slack soft-splits above 4000).
- **Slash command + event registration** via the Slack app manifest, replacing PTB `CommandHandler`.

## Explicitly cut from v1 (future work)

- **jsonl terminal-mirror** + `/respawn` + `/sync` — the phantom-re-render saga. v1 persists `session_id` for restart-resume only; it does not reflect terminal-driven turns into Slack.
- **Visual-block `.txt` attachment hack** — dropped. Slack renders ```` ``` ```` monospace fine cross-platform (including iOS), so the QuickLook workaround is unnecessary. `files_upload_v2` as a snippet is the future escape hatch if ever needed.
- **`/adopt`, `/digest`, `/todo`, `/move`, `/btw`, `/cc`, `/respawn`, `/sync`, `/deep`, `/fast`** — Telegram parity commands, deferred.
- **Chief-of-staff orchestration** (spawn/peek/send worker sessions) — deferred.
- **Per-session cwd / `.cd` / repo-picker** — deliberately omitted; the single official-repos cwd covers the stated workflow.
- **Rename / `.name`** — deferred.

## Secrets / config (`.env`)

```
SLACK_BOT_TOKEN=xoxb-...        # bot token
SLACK_APP_TOKEN=xapp-...        # Socket Mode app-level token
ALLOWED_USER_ID=U...            # only this Slack user can drive the bot
CLAUDE_CHANNEL_ID=C...          # the #claude channel
DEFAULT_CWD=/Users/noahchun/Developer/blackbird/official-repos
```

**Required scopes:** `chat:write`, `commands`, `channels:history`, `channels:read`, `pins:write` (and `groups:*` equivalents if the channel is private). **Socket Mode** enabled with an app-level token carrying `connections:write`.

## Hosting

Same Mac. A launchd plist ported from `claude-tg-bot`'s template keeps it alive across reboot/crash. `requirements.txt`: `slack_bolt`, `claude-agent-sdk`, `python-dotenv`.

## File map (target)

- `bot.py` — main, single file (right-sized for one user; don't split until a seam breaks)
- `sessions.json` — runtime state (gitignored)
- `.env` / `.env.example`
- `requirements.txt`
- `restart.sh` — ported bouncer
- `claude-slack-bot.plist.template` — launchd template
- `SETUP.md`
- `CLAUDE.md` — project brief + Slack-specific gotchas

## Known Slack gotchas to capture in CLAUDE.md during build

- Slash commands don't carry `thread_ts` → per-session controls must be in-thread messages, not slash commands.
- `chat.postMessage` is rate-limited ~1 msg/sec/channel; `chat.update` is tier-3 → debounce StatusBar edits.
- Socket Mode needs both a bot token (`xoxb-`) and an app-level token (`xapp-`).
- `mrkdwn` ≠ Markdown: single-asterisk bold, no headers, `<url|text>` link syntax.
- Shared-workspace privacy is app-layer only via `ALLOWED_USER_ID`; the bot is otherwise visible to everyone in the channel.
```
