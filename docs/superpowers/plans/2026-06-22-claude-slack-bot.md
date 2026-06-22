# claude-slack-bot Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A single-user Slack bot that wraps Claude Code via the Agent SDK, mapping each Slack thread in one channel to a Claude conversation.

**Architecture:** One `bot.py` running `slack_bolt` in Socket Mode (no public URL). A top-level channel message creates a session (thread); thread replies route to that session's `ClaudeSDKClient`. A pinned message indexes active sessions. State persists in `sessions.json`. Forked conceptually from `claude-tg-bot` — `Session`/`StatusBar`/`ReplyOnce` ported, transport + formatter + routing rewritten for Slack.

**Tech Stack:** Python 3.10+, `slack_bolt` (async, Socket Mode), `claude-agent-sdk`, `python-dotenv`, `pytest` (formatter unit tests only).

## Global Constraints

- Python 3.10+ (PEP 604 `X | None` syntax used throughout).
- `DEFAULT_CWD = /Users/noahchun/Developer/blackbird/official-repos` — every session starts here; no per-session cwd in v1.
- Single file `bot.py` — do not split into modules (right-sized for one user).
- State in `sessions.json` (JSON, single writer), gitignored.
- Allowlist: bot acts ONLY on `ALLOWED_USER_ID`; all other users ignored (logged).
- Tests only for the pure `mrkdwn` formatter (project has no broader suite; matches user convention "tests only if a suite exists" — the formatter is the one pure-logic, error-prone unit worth covering).
- Slack `mrkdwn`, not Markdown: `*bold*`, `_italic_`, `` `code` ``, ```` ```block``` ````, `>quote`, `<url|text>`, no headers.
- Per-session controls are in-thread `.`-prefixed messages (`.cancel`/`.plan`/`.auto`), NOT slash commands (slash commands lack `thread_ts`).

---

### Task 1: Repo scaffold + dependencies

**Files:**
- Create: `.gitignore`, `requirements.txt`, `.env.example`, `README.md`
- (spec already at `docs/superpowers/specs/2026-06-22-claude-slack-bot-design.md`)

- [ ] **Step 1: Init git and gitignore**

```bash
cd ~/Developer/general/claude-slack-bot
git init
printf '.env\nsessions.json\n.venv/\n__pycache__/\n*.log\n*.err.log\n' > .gitignore
```

- [ ] **Step 2: requirements.txt**

```
slack_bolt>=1.18.0
claude-agent-sdk>=0.2.0
python-dotenv>=1.0.0
```

- [ ] **Step 3: .env.example**

```
SLACK_BOT_TOKEN=xoxb-paste-from-slack-app
SLACK_APP_TOKEN=xapp-paste-from-slack-app
ALLOWED_USER_ID=U-your-slack-user-id
CLAUDE_CHANNEL_ID=C-your-channel-id
DEFAULT_CWD=/Users/noahchun/Developer/blackbird/official-repos
```

- [ ] **Step 4: venv + install**

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```
Expected: installs `slack_bolt`, `claude_agent_sdk`, `dotenv` cleanly.

- [ ] **Step 5: Commit (includes the spec as initial commit)**

```bash
git add -A && git commit -m "chore: scaffold claude-slack-bot (deps, env template, design spec)"
```

---

### Task 2: Slack app creation (manual, user-driven) + connectivity smoke test

**Files:**
- Create: `bot.py` (minimal Socket Mode skeleton), `manifest.yml` (reference, pasted into Slack UI)

**Interfaces:**
- Produces: a running Bolt `AsyncApp` + `AsyncSocketModeHandler`; env vars loaded; `app` global reused by later tasks.

- [ ] **Step 1: Write `manifest.yml` for the Slack app (user pastes into api.slack.com → Create App → From manifest)**

```yaml
display_information:
  name: claude-cc
features:
  bot_user:
    display_name: claude-cc
    always_online: true
  slash_commands:
    - command: /list
      description: Repost the pinned session index
      should_escape: false
oauth_config:
  scopes:
    bot:
      - chat:write
      - commands
      - channels:history
      - channels:read
      - pins:write
      - groups:history
      - groups:read
settings:
  socket_mode_enabled: true
  interactivity:
    is_enabled: true
```

- [ ] **Step 2: User completes Slack-side setup (document in SETUP.md later)**
  - Create app from manifest. Install to workspace → copy Bot Token (`xoxb-`) → `.env`.
  - Basic Information → App-Level Tokens → generate token with `connections:write` (`xapp-`) → `.env`.
  - Create/choose `#claude` channel, invite the bot, copy channel ID (`C…`) → `.env`.
  - Get own user ID (profile → ... → Copy member ID, `U…`) → `.env`.

- [ ] **Step 3: Minimal `bot.py` skeleton that connects and logs ready**

```python
import os, logging
from dotenv import load_dotenv
from slack_bolt.app.async_app import AsyncApp
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("claude-slack-bot")

BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]
APP_TOKEN = os.environ["SLACK_APP_TOKEN"]
ALLOWED_USER_ID = os.environ["ALLOWED_USER_ID"]
CLAUDE_CHANNEL_ID = os.environ["CLAUDE_CHANNEL_ID"]
DEFAULT_CWD = os.environ.get("DEFAULT_CWD", os.path.expanduser("~"))

app = AsyncApp(token=BOT_TOKEN)

@app.event("app_mention")
async def _ping(event, say):
    await say(text="pong", thread_ts=event.get("thread_ts") or event["ts"])

async def main():
    log.info("starting claude-slack-bot; channel=%s user=%s cwd=%s", CLAUDE_CHANNEL_ID, ALLOWED_USER_ID, DEFAULT_CWD)
    handler = AsyncSocketModeHandler(app, APP_TOKEN)
    await handler.start_async()

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
```

- [ ] **Step 4: Smoke test connectivity**

Run: `.venv/bin/python bot.py`
Then in Slack, `@claude-cc` in the channel.
Expected: log shows "starting…"; bot replies "pong" in-thread. Ctrl-C to stop.

- [ ] **Step 5: Commit**

```bash
git add bot.py manifest.yml && git commit -m "feat: Socket Mode skeleton + app manifest, connectivity verified"
```

---

### Task 3: mrkdwn formatter (UNIT TESTED — the one pure-logic unit)

**Files:**
- Modify: `bot.py` (add `to_mrkdwn(text: str) -> str`)
- Test: `tests/test_mrkdwn.py`

**Interfaces:**
- Produces: `to_mrkdwn(md: str) -> str` — converts Claude's standard Markdown to Slack mrkdwn. Used by every outbound text send.

- [ ] **Step 1: Write failing tests**

```python
# tests/test_mrkdwn.py
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from bot import to_mrkdwn

def test_bold_double_to_single_asterisk():
    assert to_mrkdwn("**bold**") == "*bold*"

def test_header_becomes_bold():
    assert to_mrkdwn("## Title") == "*Title*"

def test_link_rewritten_to_slack_syntax():
    assert to_mrkdwn("[text](http://x.com)") == "<http://x.com|text>"

def test_fenced_code_block_preserved():
    assert to_mrkdwn("```\ncode\n```") == "```\ncode\n```"

def test_inline_code_preserved():
    assert to_mrkdwn("use `x` here") == "use `x` here"

def test_code_block_content_not_reformatted():
    # double-asterisk inside a code fence must NOT become single
    assert to_mrkdwn("```\n**literal**\n```") == "```\n**literal**\n```"

def test_bullet_dash_preserved():
    assert to_mrkdwn("- item") == "- item"
```

- [ ] **Step 2: Run, verify fail**

Run: `.venv/bin/python -m pytest tests/test_mrkdwn.py -v`
Expected: FAIL — `ImportError: cannot import name 'to_mrkdwn'`

- [ ] **Step 3: Implement `to_mrkdwn` in `bot.py`**

```python
import re

_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
_HEADER_RE = re.compile(r"^#{1,6}\s+(.*)$", re.MULTILINE)
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^)]+)\)")

def to_mrkdwn(md: str) -> str:
    """Convert standard Markdown (Claude's output) to Slack mrkdwn.
    Fenced code blocks are extracted first and restored verbatim so their
    contents are never reformatted."""
    placeholders: list[str] = []
    def _stash(m: re.Match) -> str:
        placeholders.append(m.group(0))
        return f"\x00{len(placeholders)-1}\x00"
    text = _FENCE_RE.sub(_stash, md)
    text = _HEADER_RE.sub(r"*\1*", text)
    text = _LINK_RE.sub(r"<\2|\1>", text)
    text = _BOLD_RE.sub(r"*\1*", text)
    def _restore(m: re.Match) -> str:
        return placeholders[int(m.group(1))]
    text = re.sub(r"\x00(\d+)\x00", _restore, text)
    return text
```

- [ ] **Step 4: Run, verify pass**

Run: `.venv/bin/python -m pytest tests/test_mrkdwn.py -v`
Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add bot.py tests/test_mrkdwn.py && git commit -m "feat: mrkdwn formatter with unit tests"
```

---

### Task 4: Session model + sessions.json persistence

**Files:**
- Modify: `bot.py`

**Interfaces:**
- Produces:
  - `Session` dataclass: `name:str, cwd:str, client:ClaudeSDKClient, channel_id:str, thread_ts:str, mode:str="acceptEdits", model:str|None=None, busy:bool=False, session_id:str|None=None, pending_prompts:deque`.
  - `sessions: dict[str, Session]` keyed by `thread_ts`.
  - `save_state()` / `load_state()` — persist `{name, cwd, mode, model, session_id, channel_id, thread_ts}` per session to `sessions.json`.
  - `make_client(cwd, mode, resume_session_id=None) -> ClaudeSDKClient`.

- [ ] **Step 1: Add imports, dataclass, and registry to `bot.py`**

```python
import json, asyncio
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions

STATE_FILE = Path(__file__).resolve().parent / "sessions.json"

@dataclass
class Session:
    name: str
    cwd: str
    client: ClaudeSDKClient
    channel_id: str
    thread_ts: str
    mode: str = "acceptEdits"
    model: str | None = None
    busy: bool = False
    session_id: str | None = None
    pending_prompts: deque = field(default_factory=deque)

sessions: dict[str, Session] = {}  # keyed by thread_ts

def make_client(cwd: str, mode: str, resume_session_id: str | None = None) -> ClaudeSDKClient:
    opts = ClaudeAgentOptions(cwd=cwd, permission_mode=mode)
    if resume_session_id:
        opts.resume = resume_session_id
    return ClaudeSDKClient(options=opts)

def save_state() -> None:
    payload = {"sessions": {
        ts: {"name": s.name, "cwd": s.cwd, "mode": s.mode, "model": s.model,
             "session_id": s.session_id, "channel_id": s.channel_id, "thread_ts": s.thread_ts}
        for ts, s in sessions.items() if s.session_id
    }}
    try:
        STATE_FILE.write_text(json.dumps(payload, indent=2))
    except Exception as e:
        log.warning("save_state failed: %s", e)

def load_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception as e:
        log.warning("load_state failed: %s", e)
        return {}
```

- [ ] **Step 2: Verify import still runs**

Run: `.venv/bin/python -c "import bot; print(bot.Session.__dataclass_fields__.keys())"`
Expected: prints the field names including `thread_ts`, `channel_id`.

- [ ] **Step 3: Commit**

```bash
git add bot.py && git commit -m "feat: Session model + sessions.json persistence"
```

---

### Task 5: Allowlist + routing + session creation

**Files:**
- Modify: `bot.py`

**Interfaces:**
- Consumes: `Session`, `sessions`, `make_client`, `save_state`, `DEFAULT_CWD`, `ALLOWED_USER_ID`, `CLAUDE_CHANNEL_ID`.
- Produces:
  - `is_allowed(event) -> bool` — `event.get("user") == ALLOWED_USER_ID and event.get("channel") == CLAUDE_CHANNEL_ID`.
  - `slugify(text) -> str` — first ≤4 words, lowercased, non-alnum→`-`, capped 32 chars; fallback `session`.
  - message handler that: ignores non-allowed; routes thread replies to `sessions[thread_ts]`; on a top-level message creates a session and calls `drive_session` (Task 6).

- [ ] **Step 1: Add `is_allowed`, `slugify`, and the `message` event handler**

```python
import re as _re

def is_allowed(event: dict) -> bool:
    return event.get("user") == ALLOWED_USER_ID and event.get("channel") == CLAUDE_CHANNEL_ID

def slugify(text: str) -> str:
    words = text.strip().split()[:4]
    slug = _re.sub(r"[^a-z0-9]+", "-", " ".join(words).lower()).strip("-")
    return (slug[:32] or "session")

@app.event("message")
async def on_message(event, client):
    # ignore bot echoes, edits, joins, and anyone but the owner
    if event.get("subtype") or event.get("bot_id"):
        return
    if not is_allowed(event):
        if event.get("user") and event.get("channel") == CLAUDE_CHANNEL_ID:
            log.info("ignoring message from non-allowed user %s", event.get("user"))
        return
    text = (event.get("text") or "").strip()
    thread_ts = event.get("thread_ts")
    ts = event["ts"]

    if thread_ts and thread_ts in sessions:
        sess = sessions[thread_ts]
        if text.startswith("."):
            await handle_dot_command(sess, text, client)   # Task 8
            return
        await drive_session(client, sess, text)             # Task 6
        return

    if thread_ts:   # reply in a thread we don't own — ignore
        return

    # top-level message → new session rooted at this message
    name = slugify(text)
    sess = Session(name=name, cwd=DEFAULT_CWD,
                   client=make_client(DEFAULT_CWD, "acceptEdits"),
                   channel_id=event["channel"], thread_ts=ts)
    await sess.client.connect()
    sessions[ts] = sess
    log.info("created session %s rooted at thread_ts=%s", name, ts)
    await drive_session(client, sess, text)                 # Task 6
    await refresh_index(client)                              # Task 9
```

- [ ] **Step 2: Verify import + handler registration**

Run: `.venv/bin/python -c "import bot; print('ok')"`
Expected: `ok` (no NameError — `drive_session`, `handle_dot_command`, `refresh_index` are defined in later tasks; if running standalone before those exist, stub them with `async def ...: pass`).

NOTE for executor: implement Tasks 6, 8, 9 before running the live bot, OR add temporary stubs. Commit this task together with Task 6 if stubs feel wasteful.

- [ ] **Step 3: Commit**

```bash
git add bot.py && git commit -m "feat: allowlist, routing, top-level-message session creation"
```

---

### Task 6: Turn execution + streaming reply (simplified port of `_drive_session`)

**Files:**
- Modify: `bot.py`

**Interfaces:**
- Consumes: `Session`, `StatusBar` (Task 7), `to_mrkdwn`, `save_state`.
- Produces:
  - `drive_session(client, sess, text) -> None` — queues if busy, else streams a turn.
  - `say_threaded(client, channel, thread_ts, text) -> None` — chunked `chat.postMessage(mrkdwn)` with plain-text fallback.
  - `friendly_verb(tool_name, tool_input) -> str` — `📖 reading X` / `🛠️ running Y` label.
  - `capture_session_id(msg, sess)` — set `sess.session_id` from first `SystemMessage`, then `save_state()`.

This is a SIMPLIFIED port of `claude-tg-bot/bot.py:1953` — it deliberately omits the dangling-tail guard, jsonl mirror, and retry/respawn (all cut from v1).

- [ ] **Step 1: Add streaming helpers**

```python
from claude_agent_sdk import AssistantMessage, TextBlock, ToolUseBlock, ResultMessage, SystemMessage

SLACK_CHUNK = 3500

async def say_threaded(client, channel: str, thread_ts: str, text: str) -> None:
    for i in range(0, len(text), SLACK_CHUNK):
        chunk = text[i:i+SLACK_CHUNK]
        try:
            await client.chat_postMessage(channel=channel, thread_ts=thread_ts,
                                          text=to_mrkdwn(chunk), mrkdwn=True)
        except Exception as e:
            log.warning("mrkdwn send failed (%s); plain fallback", e)
            await client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=chunk)

def friendly_verb(name: str, inp: dict | None) -> str:
    inp = inp or {}
    if name in ("Read", "Glob", "Grep"):
        return f"📖 reading {inp.get('file_path') or inp.get('pattern') or name}"
    if name == "Bash":
        return f"🛠️ running `{(inp.get('command') or '')[:60]}`"
    if name in ("Write", "Edit", "MultiEdit"):
        return f"✏️ editing {inp.get('file_path') or ''}"
    return f"🛠️ {name}"

async def capture_session_id(msg, sess: Session) -> None:
    if isinstance(msg, SystemMessage) and sess.session_id is None:
        sid = getattr(msg, "data", {}).get("session_id") if hasattr(msg, "data") else None
        sid = sid or getattr(msg, "session_id", None)
        if sid:
            sess.session_id = sid
            save_state()
            log.info("captured session_id=%s for %s", sid[:8], sess.name)
```

- [ ] **Step 2: Add `drive_session`**

```python
async def drive_session(client, sess: Session, text: str) -> None:
    if sess.busy:
        sess.pending_prompts.append(text)
        await client.chat_postMessage(channel=sess.channel_id, thread_ts=sess.thread_ts,
                                      text=f"📥 queued ({len(sess.pending_prompts)} pending)")
        return
    sess.busy = True
    status = StatusBar(client, sess.channel_id, sess.thread_ts)
    await status.start()
    streamed = False
    try:
        await sess.client.query(text)
        async for msg in sess.client.receive_response():
            await capture_session_id(msg, sess)
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock) and block.text.strip():
                        if not streamed:
                            await status.stop()
                            streamed = True
                        await say_threaded(client, sess.channel_id, sess.thread_ts, block.text)
                    elif isinstance(block, ToolUseBlock):
                        await status.update(friendly_verb(block.name, getattr(block, "input", None)))
            elif isinstance(msg, ResultMessage):
                break
    except Exception as e:
        log.exception("turn failed for %s", sess.name)
        await status.stop()
        await say_threaded(client, sess.channel_id, sess.thread_ts, f"⚠️ error: {e}")
    finally:
        await status.stop()
        sess.busy = False
        if sess.pending_prompts:
            nxt = sess.pending_prompts.popleft()
            await drive_session(client, sess, nxt)
```

- [ ] **Step 3: Live smoke test** — start bot, post a top-level message "say hi in one word" in `#claude`.
Expected: a `🟢`-style status appears then is replaced by the assistant reply in-thread; `sessions.json` gains an entry with a `session_id`.

- [ ] **Step 4: Commit**

```bash
git add bot.py && git commit -m "feat: simplified turn loop + streaming threaded replies"
```

---

### Task 7: StatusBar (chat.update-based)

**Files:**
- Modify: `bot.py`

**Interfaces:**
- Consumes: bolt `client` (web client), channel, thread_ts.
- Produces: `StatusBar` with `start()`, `update(label)`, `stop()`. `update` debounced ≥1s. `start` posts a placeholder message; `stop` deletes it.

- [ ] **Step 1: Add StatusBar**

```python
import time

class StatusBar:
    """A single thread message that shows live tool activity, deleted once
    real assistant text begins. Debounced to respect chat.update limits."""
    def __init__(self, client, channel: str, thread_ts: str):
        self.client, self.channel, self.thread_ts = client, channel, thread_ts
        self.ts: str | None = None
        self._last_edit = 0.0

    async def start(self) -> None:
        try:
            r = await self.client.chat_postMessage(channel=self.channel, thread_ts=self.thread_ts, text="💭 thinking…")
            self.ts = r["ts"]
        except Exception as e:
            log.warning("StatusBar.start failed: %s", e)

    async def update(self, label: str) -> None:
        if not self.ts:
            return
        now = time.monotonic()
        if now - self._last_edit < 1.0:
            return
        self._last_edit = now
        try:
            await self.client.chat_update(channel=self.channel, ts=self.ts, text=label)
        except Exception as e:
            log.debug("StatusBar.update failed: %s", e)

    async def stop(self) -> None:
        if not self.ts:
            return
        try:
            await self.client.chat_delete(channel=self.channel, ts=self.ts)
        except Exception as e:
            log.debug("StatusBar.stop failed: %s", e)
        finally:
            self.ts = None
```

- [ ] **Step 2: Live test** — post a message that triggers tool use ("read README.md and summarize").
Expected: status message updates to `📖 reading …` then disappears when prose starts.

- [ ] **Step 3: Commit**

```bash
git add bot.py && git commit -m "feat: StatusBar via chat.update/chat.delete"
```

---

### Task 8: Per-session dot-commands (.cancel / .plan / .auto)

**Files:**
- Modify: `bot.py`

**Interfaces:**
- Consumes: `Session`, bolt `client`.
- Produces: `handle_dot_command(sess, text, client) -> None`.

- [ ] **Step 1: Add handler**

```python
async def handle_dot_command(sess: Session, text: str, client) -> None:
    cmd = text[1:].strip().split()[0].lower() if len(text) > 1 else ""
    if cmd == "cancel":
        try:
            await sess.client.interrupt()
            msg = "🛑 cancelled"
        except Exception as e:
            msg = f"cancel failed: {e}"
    elif cmd == "plan":
        sess.mode = "plan"
        await sess.client.set_permission_mode("plan")
        save_state()
        msg = "🧭 plan mode"
    elif cmd == "auto":
        sess.mode = "acceptEdits"
        await sess.client.set_permission_mode("acceptEdits")
        save_state()
        msg = "⚡ auto (acceptEdits) mode"
    else:
        msg = f"unknown command: .{cmd} (try .cancel/.plan/.auto)"
    await client.chat_postMessage(channel=sess.channel_id, thread_ts=sess.thread_ts, text=msg)
```

NOTE: verify the SDK exposes `interrupt()` and `set_permission_mode()` on `ClaudeSDKClient`; in `claude-tg-bot` these are used at `bot.py:1696` (cancel) and `bot.py:1872` (plan). If the method name differs in the installed SDK version, match the working call in `claude-tg-bot/bot.py`.

- [ ] **Step 2: Live test** — in a session thread send `.plan`, then `.auto`, then start a long turn and `.cancel`.
Expected: confirmations posted; cancel interrupts mid-turn.

- [ ] **Step 3: Commit**

```bash
git add bot.py && git commit -m "feat: in-thread .cancel/.plan/.auto controls"
```

---

### Task 9: Pinned session index + /list

**Files:**
- Modify: `bot.py`

**Interfaces:**
- Consumes: `sessions`, bolt `client`, `CLAUDE_CHANNEL_ID`.
- Produces:
  - `index_state: dict` holding the index message ts (persisted in sessions.json under `"index_ts"`).
  - `refresh_index(client) -> None` — builds the index text from `sessions` (each session: status dot + name + permalink via `chat_getPermalink`), upserts a single channel message, and pins it.
  - `/list` slash handler calling `refresh_index`.

- [ ] **Step 1: Add index logic**

```python
index_ts: str | None = None  # module-level; loaded/saved with state

async def refresh_index(client) -> None:
    global index_ts
    lines = ["*Sessions*"]
    for ts, s in sessions.items():
        dot = "🟡" if s.busy else "🟢"
        try:
            link = (await client.chat_getPermalink(channel=s.channel_id, message_ts=ts))["permalink"]
            lines.append(f"{dot} <{link}|{s.name}>")
        except Exception:
            lines.append(f"{dot} {s.name}")
    text = "\n".join(lines) if len(lines) > 1 else "*Sessions*\n_none yet — send a message to start one_"
    try:
        if index_ts:
            await client.chat_update(channel=CLAUDE_CHANNEL_ID, ts=index_ts, text=text)
        else:
            r = await client.chat_postMessage(channel=CLAUDE_CHANNEL_ID, text=text)
            index_ts = r["ts"]
            await client.pins_add(channel=CLAUDE_CHANNEL_ID, timestamp=index_ts)
        save_state_index()
    except Exception as e:
        log.warning("refresh_index failed: %s", e)

def save_state_index() -> None:
    # extend save_state payload with index_ts; simplest: re-read, patch, write
    data = load_state()
    data["index_ts"] = index_ts
    STATE_FILE.write_text(json.dumps(data, indent=2))

@app.command("/list")
async def cmd_list(ack, client):
    await ack()
    await refresh_index(client)
```

NOTE: fold `index_ts` into `save_state()`/`load_state()` cleanly during implementation rather than the patch-shim above if it reads better — keep one writer to `sessions.json`.

- [ ] **Step 2: Live test** — create two sessions, run `/list`.
Expected: one pinned message lists both with clickable names; busy session shows 🟡.

- [ ] **Step 3: Commit**

```bash
git add bot.py && git commit -m "feat: pinned session index + /list"
```

---

### Task 10: Restart-resume, restart.sh, launchd plist, SETUP.md, CLAUDE.md

**Files:**
- Create: `restart.sh`, `claude-slack-bot.plist.template`, `SETUP.md`, `CLAUDE.md`
- Modify: `bot.py` (rehydrate sessions from `sessions.json` on startup)

- [ ] **Step 1: On startup, rebuild sessions from state (resume clients)**

```python
async def restore_sessions() -> None:
    global index_ts
    data = load_state()
    index_ts = data.get("index_ts")
    for ts, sd in data.get("sessions", {}).items():
        c = make_client(sd["cwd"], sd.get("mode", "acceptEdits"), resume_session_id=sd.get("session_id"))
        await c.connect()
        sessions[ts] = Session(name=sd["name"], cwd=sd["cwd"], client=c,
                               channel_id=sd["channel_id"], thread_ts=ts,
                               mode=sd.get("mode", "acceptEdits"),
                               model=sd.get("model"), session_id=sd.get("session_id"))
    log.info("restored %d sessions", len(sessions))
```
Call `await restore_sessions()` at the top of `main()` before `handler.start_async()`.

- [ ] **Step 2: restart.sh (ported bouncer)**

```bash
#!/bin/sh
# Restart the bot, surviving SIGHUP from the caller (incl. from inside the SDK).
DELAY="${1:-0}"
DIR="$(cd "$(dirname "$0")" && pwd)"
( sleep "$DELAY"
  pkill -f "$DIR/bot.py" 2>/dev/null
  sleep 1
  ( cd "$DIR" && nohup .venv/bin/python bot.py >> bot.log 2>> bot.err.log & )
) &
```
`chmod +x restart.sh`

- [ ] **Step 3: launchd template** (port from `claude-tg-bot/claude-tg-bot.plist.template`, swap label/paths to `claude-slack-bot`).

- [ ] **Step 4: Write SETUP.md** (manifest install, tokens, scopes, channel/user IDs, smoke test, launchd) and **CLAUDE.md** (architecture + the Slack gotchas list from the spec).

- [ ] **Step 5: Restart-resume test** — create a session, restart the bot, post in that thread.
Expected: reply continues the existing conversation (same `session_id`, history intact).

- [ ] **Step 6: Commit**

```bash
git add -A && git commit -m "feat: restart-resume, restart.sh, launchd template, SETUP.md, CLAUDE.md"
```

---

## Self-Review

**Spec coverage:**
- channel + threads-as-sessions → Tasks 5, 6 ✓
- pinned index / `/list` → Task 9 ✓
- top-level msg creation, auto-name, official-repos cwd → Task 5 ✓
- `.cancel/.plan/.auto`, slash-vs-dot rationale → Task 8 ✓
- StatusBar / ReplyOnce (thread anchoring is implicit — every send uses `thread_ts`) → Tasks 6, 7 ✓
- mrkdwn formatter → Task 3 ✓
- allowlist → Task 5 ✓
- sessions.json persistence + restart resume → Tasks 4, 10 ✓
- Socket Mode transport + scopes/manifest → Task 2 ✓
- launchd hosting → Task 10 ✓
- Cut items (mirror, adopt, digest, todo, orchestration, per-session cwd) → absent by design ✓

**Placeholder scan:** No TBDs. Two NOTEs flag SDK-method-name verification (Task 8) and an index-persistence refactor preference (Task 9) — both point at the exact working reference in `claude-tg-bot/bot.py`, not vague "handle it."

**Type consistency:** `Session` fields (Task 4) match usage in Tasks 5–10. `drive_session(client, sess, text)`, `handle_dot_command(sess, text, client)`, `refresh_index(client)`, `say_threaded(client, channel, thread_ts, text)`, `StatusBar(client, channel, thread_ts)` signatures consistent across tasks. `thread_ts` is the session key everywhere.
