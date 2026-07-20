# claude-slack-bot

A Slack bot that wraps Claude Code via the Agent SDK. Single-user (per install),
long-lived Python process on macOS/Linux. Lives in one Slack channel; each
top-level message starts a Claude session bound to that message's thread.

Fork of `claude-tg-bot` (a sibling repo). The `Session`/`StatusBar`/`ReplyOnce`
concepts and `sessions.json` persistence are ported; the transport (Telegram →
Slack Socket Mode), formatter (MarkdownV2 → mrkdwn), and routing (topic →
thread) are rewritten. See `docs/superpowers/specs/` for the design and
`docs/superpowers/plans/` for the build plan.

## Core model

- **Session** — one `ClaudeSDKClient`. Keyed by `thread_ts` in `sessions: dict[str, Session]`. Persists across restarts via `session_id` captured from the first `SystemMessage`; written to `sessions.json` and resumed on startup (`restore_sessions`).
- **Channel = control surface.** The bot lives in several home channels (`HOME_CHANNELS`); in any of them a top-level (non-threaded, non-command) message creates a session rooted at its `ts`, and thread replies route to `sessions[thread_ts]`. In non-home channels an explicit @-mention is required to start one.
- **Pinned session index** — a single channel message (`index_ts`) listing active sessions as clickable permalinks (🟢 idle / 🟡 busy). The `/list` analog; refreshed on session create and on each turn's start/end.
- **StatusBar** — a thread message posted at turn start, updated with `📖 reading X` / `🛠️ running Y` via `chat.update`, deleted when real assistant text begins. Debounced ≥1s/edit.
- **cwd + channel posture** — each channel has a profile (`CHANNEL_PROFILES`) giving new sessions a landing `cwd` and a `purpose` string appended to Claude Code's default system prompt (preset+append, never replacing it). #infra → the harness repo, #main → `~/Developer/blackbird`, #fixes → `DEFAULT_CWD`, #side-projects → `~/Developer/personal`. Unlisted channels fall back to `DEFAULT_CWD` (defaults to `~`) with no posture; a profile whose cwd is missing on the host also falls back. cwd is a starting context, not a boundary — full tool access works across everything underneath. Posture is re-derived from the channel on restart, never persisted to `sessions.json`.

## Command surface

- top-level message → new session (auto-named via `slugify`)
- thread reply → talk to that session
- `.cancel` / `.plan` / `.auto` (in-thread) → per-session controls
- `/list` (slash) → repost the pinned index

## Gotchas learned the hard way

- **Slack slash commands do NOT carry `thread_ts`.** A `/cancel` typed in a thread can't tell the bot which session it targets. So per-session controls are in-thread `.`-prefixed *messages* (read off the `message` event, which DOES include `thread_ts`), NOT slash commands. Only thread-agnostic actions (`/list`) are real slash commands.
- **Async Bolt needs `aiohttp`.** `slack_bolt` has no `[async]` extra in current versions; install `aiohttp` explicitly (it's in `requirements.txt`).
- **A long turn must not block other sessions.** The `message` handler dispatches each turn via `asyncio.create_task` and returns immediately — otherwise Bolt processes events serially and one session's turn freezes the rest. Same-session ordering is still safe: the `busy` check-and-set in `drive_session` is atomic under asyncio (no `await` between them), so a second message to a busy session queues correctly.
- **Two instances both hold Socket Mode connections** and double-process every event → duplicate replies. Keep exactly one (`pgrep -lf bot.py`); use `./restart.sh` (delegates to launchd `kickstart`).
- **`bot.py` reads required env vars at import time.** Tests set dummy env before `import bot` (see `tests/test_mrkdwn.py`).
- **mrkdwn ≠ Markdown.** `*bold*` is single-asterisk, `_italic_`, no headers (rendered as bold), links are `<url|text>`. `to_mrkdwn` stashes fenced code blocks first so their contents are never reformatted.
- **Privacy is app-layer only** via `ALLOWED_USER_ID`. On a shared workspace the bot is visible to everyone in the channel; they can watch but only the owner drives. The allowlist is load-bearing — the bot runs Claude Code with full host access.
- **`session_id` is captured from the first `SystemMessage`** as `getattr(msg, "session_id", None) or (msg.data or {}).get("session_id")`. Only persisted sessions (those with an id) survive restart.

## Restarting (Claude may do this autonomously)

The owner has granted standing agency to restart the bot when a change requires
it — **don't ask first** (granted 2026-07-20). Restart when you've committed a
change to `bot.py` or other loaded harness code that needs a reload; skip it for
doc-only or unrelated changes.

- **How:** `./restart.sh <delay>` from the repo, e.g. `./restart.sh 15`. It kills
  the running bot by discovered PID and relaunches, detached — safe to run from
  *inside* the bot's own session (it survives its own SIGHUP).
- **Reply BEFORE you trigger it.** The restart terminates the current session
  (you are a child of `bot.py`), so post your message first, then call
  `./restart.sh 15` as the last action — the delay lets the reply land before the
  bounce. Persisted sessions resume on startup via `restore_sessions`.
- **Don't use `pkill -f` yourself.** On macOS the bot runs as homebrew-python
  with `bot.py` as a relative argv the pattern can't see — that's why `restart.sh`
  discovers the PID via `ps -o command | grep ' bot.py'` instead.
- **Verify after:** `grep "starting claude-slack-bot" bot.log | tail -1` shows a
  fresh timestamp, and `ps ax -o pid=,command= | grep -F ' bot.py' | grep -v grep`
  shows exactly one homebrew-python instance (extra matches under `.venv/…/claude`
  are transient SDK subprocesses, fine). You can't verify within the same turn —
  the restart kills you — so verification happens on the owner's next message.

## Cut from v1 (future)

jsonl terminal-mirror / `/respawn` / `/sync`; `/adopt`, `/digest`, `/todo`,
`/move`, `/btw`, `/cc`; chief-of-staff orchestration; per-session cwd / `.cd`;
rename. The visual-block `.txt` attachment hack is intentionally dropped (Slack
renders monospace code fine across platforms).

## File map

- `bot.py` — main, single file (right-sized for one user; don't split until a seam breaks)
- `sessions.json` — runtime state (gitignored)
- `manifest.yml` — Slack app manifest (paste into api.slack.com)
- `.env` / `.env.example` — tokens, allowed user, channel, default cwd
- `restart.sh` — bouncer that survives SIGHUP / delegates to launchd
- `claude-slack-bot.plist.template` — launchd template
- `tests/test_mrkdwn.py` — formatter unit tests (the one pure-logic unit)
- `SETUP.md` — install instructions

## When in doubt

- Check `bot.log` / `bot.err.log` first.
- `pgrep -lf bot.py` to confirm one instance.
- `cat sessions.json | python3 -m json.tool` for state.
- Run formatter tests: `.venv/bin/python -m pytest tests/ -v`.
