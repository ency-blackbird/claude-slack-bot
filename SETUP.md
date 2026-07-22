# Setup

A Slack bot that wraps Claude Code. One channel = your control surface; each
top-level message starts a Claude session (a thread); thread replies continue
it. A pinned message indexes active sessions.

## Requirements

| Requirement | Notes |
|---|---|
| **macOS or Linux** | macOS tested; swap the launchd plist for systemd on Linux. |
| **Python 3.10+** | Uses `X \| None` syntax. |
| **Claude Code CLI** | The bundled `claude-agent-sdk` runs the local `claude` binary and inherits its auth. Either a Claude Pro/Max login or `ANTHROPIC_API_KEY` in the env. |
| **A Slack workspace where you can install apps** | Admin or app-install permission. |

## 1. Create the Slack app

1. Go to <https://api.slack.com/apps> → **Create New App** → **From an app manifest**.
2. Pick your workspace, paste the contents of `manifest.yml`, create.
3. **Install to Workspace** → authorize → copy the **Bot User OAuth Token** (`xoxb-…`).
4. **Basic Information → App-Level Tokens → Generate Token and Scopes**: add the
   `connections:write` scope, generate, copy the token (`xapp-…`). This is what
   Socket Mode uses — no public URL needed.

## 2. Create the channel and gather IDs

1. Create (or pick) a channel, e.g. `#claude`. Invite the bot: `/invite @your-bot` (the name you gave it in the manifest).
2. Channel ID: open the channel → click its name → bottom of the popover shows
   the ID (`C…`). Or right-click the channel → Copy link; the ID is the last path segment.
3. Your user ID: click your avatar → Profile → ⋯ → **Copy member ID** (`U…`).

## 3. Install deps, configure, and run

```sh
cd claude-slack-bot
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
```

Edit `.env`:

```
SLACK_BOT_TOKEN=xoxb-...
SLACK_APP_TOKEN=xapp-...
ALLOWED_USER_ID=U...            # only this user can drive the bot
CLAUDE_CHANNEL_ID=C...          # the bot's primary "home" channel
DEFAULT_CWD=/Users/you/code     # absolute path where new sessions start (NOT ~-expanded)
WRITE_ROOT=~/code               # writes outside this tree ask for approval (default: ~/Developer)
```

`WRITE_ROOT` is the safety boundary: edits/writes to absolute paths outside it
pop a ✅/❌ approval button in the thread, while everything inside runs silently.
Point it at wherever your real work lives (omit it to keep the `~/Developer`
default). `DEFAULT_CWD` is just where a fresh session lands — give it an absolute
path, since it is not `~`-expanded.

Smoke test:

```sh
.venv/bin/python bot.py
```

In your channel, @-mention the bot — it should reply that it's here. Then send a
plain top-level message like `say hi in one word`. The bot creates a session
thread, shows a status line while it works, and replies in-thread.

## Day-to-day

| Action | Where | Effect |
|---|---|---|
| top-level message | channel | start a new session (auto-named), prompt = the message |
| reply in a thread | in-thread | continue that session |
| `.cancel` | in-thread | interrupt the current turn |
| `.plan` | in-thread | switch the session to plan mode |
| `.auto` | in-thread | switch the session back to acceptEdits |
| `/list` | anywhere in channel | repost/refresh the pinned session index |

Every session runs in `DEFAULT_CWD`, giving cross-repo access. Tell a session
which repo to work in as part of your message.

## (Optional) Multiple channels with distinct postures

The bot can live in several channels at once, each with its own landing
directory and behavioral "posture" (a system-prompt append that tells a fresh
session what that channel is FOR). This is configured near the top of `bot.py`:

- `EXTRA_HOME_CHANNELS` — channel IDs (besides `CLAUDE_CHANNEL_ID`) where a plain
  top-level message starts a session with no @-mention. Anywhere else, the bot
  stays quiet unless @-mentioned.
- `CHANNEL_PROFILES` — maps a channel ID to `{name, cwd, purpose}`: where new
  sessions land and the posture appended to the system prompt. Unlisted channels
  fall back to `DEFAULT_CWD` with no posture; a profile whose `cwd` is missing on
  your machine also falls back, so leftover example paths won't break anything.

The committed values are a worked example (four channels: infra / fixes / main /
side-projects). Swap in your own channel IDs, paths, and purpose text. Channel
IDs aren't secret, so they live in code for review visibility. Editing `bot.py`
requires a restart (`./restart.sh`).

## (Optional) Auto-restart on macOS (launchd)

```sh
cp claude-slack-bot.plist.template ~/Library/LaunchAgents/com.$(whoami).claude-slack-bot.plist
sed -i '' "s|__BOT_DIR__|$(pwd)|g; s|__USER__|$(whoami)|g" ~/Library/LaunchAgents/com.$(whoami).claude-slack-bot.plist
launchctl load ~/Library/LaunchAgents/com.$(whoami).claude-slack-bot.plist
launchctl list | grep claude-slack-bot
```

Tail logs: `tail -f bot.log bot.err.log`. Restart: `./restart.sh [delay_sec]`.
Unload: `launchctl unload ~/Library/LaunchAgents/com.$(whoami).claude-slack-bot.plist`.

## Troubleshooting

**Bot connects but never replies to your messages** — check `ALLOWED_USER_ID`
and `CLAUDE_CHANNEL_ID` match exactly (the bot ignores everyone/everywhere else,
by design). The log prints `ignoring message from non-allowed user …` when it drops one.

**Duplicate replies** — two bot processes are both holding Socket Mode
connections. `pgrep -lf bot.py` should show exactly one; kill extras. If using
launchd, use `./restart.sh` (delegates to `kickstart`) rather than launching by hand.

**`not_in_channel` / `channel_not_found`** — invite the bot to the channel.

**Bot starts then dies** — almost always a missing `.env` var; check `bot.err.log`.
