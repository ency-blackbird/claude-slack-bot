"""claude-slack-bot — wraps Claude Code (Agent SDK) behind one Slack channel.

Model: one channel (#claude). A top-level message starts a session rooted at
that message's thread; thread replies route to that session. A pinned message
indexes active sessions. Single-user: only ALLOWED_USER_ID can drive the bot.

Fork of claude-tg-bot — Session/StatusBar concepts ported, transport +
formatter + routing rewritten for Slack. See docs/superpowers/specs/.
"""

import asyncio
import json
import logging
import os
import re
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv
from slack_bolt.app.async_app import AsyncApp
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolUseBlock,
)

# ---------- config ----------

load_dotenv()
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
)
log = logging.getLogger("claude-slack-bot")

BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]
APP_TOKEN = os.environ["SLACK_APP_TOKEN"]
ALLOWED_USER_ID = os.environ["ALLOWED_USER_ID"]
CLAUDE_CHANNEL_ID = os.environ["CLAUDE_CHANNEL_ID"]
DEFAULT_CWD = os.environ.get("DEFAULT_CWD", os.path.expanduser("~"))

STATE_FILE = Path(__file__).resolve().parent / "sessions.json"
SLACK_CHUNK = 3500  # Slack soft-splits text above ~4000 chars

app = AsyncApp(token=BOT_TOKEN)


# ---------- mrkdwn formatter (pure; unit-tested) ----------

_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
_HEADER_RE = re.compile(r"^#{1,6}\s+(.*)$", re.MULTILINE)
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^)]+)\)")


def to_mrkdwn(md: str) -> str:
    """Convert standard Markdown (Claude's output) to Slack mrkdwn.

    Fenced code blocks are extracted first and restored verbatim so their
    contents are never reformatted (e.g. `**literal**` inside a fence stays).
    Headers -> bold, [t](url) -> <url|t>, **b** -> *b*.
    """
    placeholders: list[str] = []

    def _stash(m: re.Match) -> str:
        placeholders.append(m.group(0))
        return f"\x00{len(placeholders) - 1}\x00"

    text = _FENCE_RE.sub(_stash, md)
    text = _HEADER_RE.sub(r"*\1*", text)
    text = _LINK_RE.sub(r"<\2|\1>", text)
    text = _BOLD_RE.sub(r"*\1*", text)

    def _restore(m: re.Match) -> str:
        return placeholders[int(m.group(1))]

    return re.sub(r"\x00(\d+)\x00", _restore, text)


# ---------- session model + persistence ----------


@dataclass
class Session:
    name: str
    cwd: str
    client: ClaudeSDKClient
    channel_id: str
    thread_ts: str  # the session key
    mode: str = "acceptEdits"
    model: str | None = None
    busy: bool = False
    session_id: str | None = None  # captured from first SystemMessage
    pending_prompts: deque = field(default_factory=deque)


sessions: dict[str, Session] = {}  # keyed by thread_ts
index_ts: str | None = None  # ts of the pinned session-index message


def make_client(
    cwd: str, mode: str, resume_session_id: str | None = None,
    model: str | None = None,
) -> ClaudeSDKClient:
    opts = ClaudeAgentOptions(cwd=cwd, permission_mode=mode)
    if resume_session_id:
        opts.resume = resume_session_id
    if model:
        opts.model = model
    return ClaudeSDKClient(options=opts)


def save_state() -> None:
    payload = {
        "sessions": {
            ts: {
                "name": s.name,
                "cwd": s.cwd,
                "mode": s.mode,
                "model": s.model,
                "session_id": s.session_id,
                "channel_id": s.channel_id,
                "thread_ts": s.thread_ts,
            }
            for ts, s in sessions.items()
            if s.session_id
        },
        "index_ts": index_ts,
    }
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


# ---------- allowlist + helpers ----------


def is_allowed(event: dict) -> bool:
    return (
        event.get("user") == ALLOWED_USER_ID
        and event.get("channel") == CLAUDE_CHANNEL_ID
    )


def slugify(text: str) -> str:
    words = text.strip().split()[:4]
    slug = re.sub(r"[^a-z0-9]+", "-", " ".join(words).lower()).strip("-")
    return slug[:32] or "session"


def friendly_verb(name: str, inp: dict | None) -> str:
    inp = inp or {}
    if name in ("Read", "Glob", "Grep"):
        return f"📖 reading {inp.get('file_path') or inp.get('pattern') or name}"
    if name == "Bash":
        return f"🛠️ running `{(inp.get('command') or '')[:60]}`"
    if name in ("Write", "Edit", "MultiEdit"):
        return f"✏️ editing {inp.get('file_path') or ''}"
    if name == "Task":
        return "🤖 dispatching subagent"
    return f"🛠️ {name}"


async def capture_session_id(msg, sess: Session) -> None:
    if not isinstance(msg, SystemMessage) or sess.session_id is not None:
        return
    sid = getattr(msg, "session_id", None) or (getattr(msg, "data", None) or {}).get(
        "session_id"
    )
    if sid:
        sess.session_id = sid
        save_state()
        log.info("captured session_id=%s for %s", sid[:8], sess.name)


# ---------- outbound ----------


async def say_threaded(client, channel: str, thread_ts: str, text: str) -> None:
    """Post text into a thread, chunked, with mrkdwn and plain-text fallback."""
    for i in range(0, len(text), SLACK_CHUNK):
        chunk = text[i : i + SLACK_CHUNK]
        try:
            await client.chat_postMessage(
                channel=channel, thread_ts=thread_ts, text=to_mrkdwn(chunk),
                mrkdwn=True,
            )
        except Exception as e:
            log.warning("mrkdwn send failed (%s); plain fallback", e)
            try:
                await client.chat_postMessage(
                    channel=channel, thread_ts=thread_ts, text=chunk
                )
            except Exception as e2:
                log.error("plain send also failed: %s", e2)


# ---------- StatusBar ----------


class StatusBar:
    """A single thread message showing live tool activity, deleted once real
    assistant text begins. Debounced to respect chat.update rate limits."""

    def __init__(self, client, channel: str, thread_ts: str):
        self.client = client
        self.channel = channel
        self.thread_ts = thread_ts
        self.ts: str | None = None
        self._last_edit = 0.0

    async def start(self) -> None:
        try:
            r = await self.client.chat_postMessage(
                channel=self.channel, thread_ts=self.thread_ts, text="💭 thinking…"
            )
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
            await self.client.chat_update(
                channel=self.channel, ts=self.ts, text=label
            )
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


# ---------- turn execution ----------


async def drive_session(client, sess: Session, text: str) -> None:
    """Send `text` to `sess` and stream the response into its thread.

    Simplified port of claude-tg-bot's _drive_session: no dangling-tail guard,
    no jsonl mirror, no retry/respawn (all cut from v1). If the session is
    busy, the prompt is queued FIFO and drained when the current turn ends.
    """
    if sess.busy:
        sess.pending_prompts.append(text)
        try:
            await client.chat_postMessage(
                channel=sess.channel_id, thread_ts=sess.thread_ts,
                text=f"📥 queued ({len(sess.pending_prompts)} pending)",
            )
        except Exception:
            pass
        return

    sess.busy = True
    await refresh_index(client)
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
                        await say_threaded(
                            client, sess.channel_id, sess.thread_ts, block.text
                        )
                    elif isinstance(block, ToolUseBlock):
                        await status.update(
                            friendly_verb(block.name, getattr(block, "input", None))
                        )
            elif isinstance(msg, ResultMessage):
                break
    except Exception as e:
        log.exception("turn failed for %s", sess.name)
        await status.stop()
        await say_threaded(
            client, sess.channel_id, sess.thread_ts, f"⚠️ error: {e}"
        )
    finally:
        await status.stop()
        sess.busy = False
        await refresh_index(client)
        if sess.pending_prompts:
            nxt = sess.pending_prompts.popleft()
            await drive_session(client, sess, nxt)


# ---------- per-session dot-commands ----------


async def handle_dot_command(sess: Session, text: str, client) -> None:
    parts = text[1:].strip().split()
    cmd = parts[0].lower() if parts else ""
    if cmd == "cancel":
        try:
            await sess.client.interrupt()
            msg = "🛑 cancelled"
        except Exception as e:
            msg = f"cancel failed: {e}"
    elif cmd == "plan":
        sess.mode = "plan"
        try:
            await sess.client.set_permission_mode("plan")
            save_state()
            msg = "🧭 plan mode"
        except Exception as e:
            msg = f"plan switch failed: {e}"
    elif cmd == "auto":
        sess.mode = "acceptEdits"
        try:
            await sess.client.set_permission_mode("acceptEdits")
            save_state()
            msg = "⚡ auto (acceptEdits) mode"
        except Exception as e:
            msg = f"auto switch failed: {e}"
    else:
        msg = f"unknown command: .{cmd} (try .cancel / .plan / .auto)"
    try:
        await client.chat_postMessage(
            channel=sess.channel_id, thread_ts=sess.thread_ts, text=msg
        )
    except Exception:
        pass


# ---------- session index ----------


async def refresh_index(client) -> None:
    """Upsert and pin a single channel message listing active sessions, each
    a clickable permalink. This is the /list analog (always-visible)."""
    global index_ts
    lines = ["*Sessions*"]
    for ts, s in sessions.items():
        dot = "🟡" if s.busy else "🟢"
        try:
            link = (
                await client.chat_getPermalink(channel=s.channel_id, message_ts=ts)
            )["permalink"]
            lines.append(f"{dot} <{link}|{s.name}>")
        except Exception:
            lines.append(f"{dot} {s.name}")
    text = (
        "\n".join(lines)
        if len(lines) > 1
        else "*Sessions*\n_none yet — send a message in this channel to start one_"
    )
    try:
        if index_ts:
            await client.chat_update(channel=CLAUDE_CHANNEL_ID, ts=index_ts, text=text)
        else:
            r = await client.chat_postMessage(channel=CLAUDE_CHANNEL_ID, text=text)
            index_ts = r["ts"]
            try:
                await client.pins_add(
                    channel=CLAUDE_CHANNEL_ID, timestamp=index_ts
                )
            except Exception as e:
                log.debug("pin failed (already pinned?): %s", e)
            save_state()
    except Exception as e:
        log.warning("refresh_index failed: %s", e)


# ---------- event handlers ----------


@app.event("message")
async def on_message(event, client):
    # ignore edits/joins/echoes and anyone but the owner; dispatch real work
    # to a background task so a long turn never blocks other sessions.
    if event.get("subtype") or event.get("bot_id"):
        return
    if not is_allowed(event):
        if event.get("user") and event.get("channel") == CLAUDE_CHANNEL_ID:
            log.info("ignoring message from non-allowed user %s", event.get("user"))
        return
    asyncio.create_task(_route(client, event))


async def _route(client, event: dict) -> None:
    try:
        text = (event.get("text") or "").strip()
        thread_ts = event.get("thread_ts")
        ts = event["ts"]

        if thread_ts and thread_ts in sessions:
            sess = sessions[thread_ts]
            if text.startswith("."):
                await handle_dot_command(sess, text, client)
            else:
                await drive_session(client, sess, text)
            return

        if thread_ts:  # reply in a thread we don't own — ignore
            return

        if not text:
            return

        # top-level message → new session rooted at this message
        name = slugify(text)
        sess = Session(
            name=name,
            cwd=DEFAULT_CWD,
            client=make_client(DEFAULT_CWD, "acceptEdits"),
            channel_id=event["channel"],
            thread_ts=ts,
        )
        await sess.client.connect()
        sessions[ts] = sess
        log.info("created session %s rooted at thread_ts=%s", name, ts)
        await drive_session(client, sess, text)
    except Exception:
        log.exception("error processing message")


@app.event("app_mention")
async def on_mention(event, client):
    # lightweight liveness ping; real work goes through plain messages
    await client.chat_postMessage(
        channel=event["channel"],
        thread_ts=event.get("thread_ts") or event["ts"],
        text="👋 I'm here. Send a top-level message to start a session.",
    )


@app.command("/list")
async def cmd_list(ack, client):
    await ack()
    await refresh_index(client)


# ---------- startup ----------


async def restore_sessions() -> None:
    """Rebuild sessions from sessions.json, resuming each Claude client by its
    persisted session_id so conversations survive a bot restart."""
    global index_ts
    data = load_state()
    index_ts = data.get("index_ts")
    for ts, sd in data.get("sessions", {}).items():
        try:
            c = make_client(
                sd["cwd"], sd.get("mode", "acceptEdits"),
                resume_session_id=sd.get("session_id"), model=sd.get("model"),
            )
            await c.connect()
            sessions[ts] = Session(
                name=sd["name"],
                cwd=sd["cwd"],
                client=c,
                channel_id=sd["channel_id"],
                thread_ts=ts,
                mode=sd.get("mode", "acceptEdits"),
                model=sd.get("model"),
                session_id=sd.get("session_id"),
            )
        except Exception:
            log.exception("failed to restore session %s", sd.get("name"))
    log.info("restored %d sessions", len(sessions))


async def main() -> None:
    log.info(
        "starting claude-slack-bot; channel=%s user=%s cwd=%s",
        CLAUDE_CHANNEL_ID, ALLOWED_USER_ID, DEFAULT_CWD,
    )
    await restore_sessions()
    handler = AsyncSocketModeHandler(app, APP_TOKEN)
    await handler.start_async()


if __name__ == "__main__":
    asyncio.run(main())
