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
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolPermissionContext,
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

# Channels skunk "lives in": a top-level message here starts a session with NO
# @-mention required. CLAUDE_CHANNEL_ID is always included; add more work
# channels below by id (the name comment is for humans — Slack routes on id).
# Channel ids aren't sensitive, so they live in code for review visibility
# (cf. skunk-hamlet's ignore-channels.ts). Anywhere NOT listed, skunk stays
# quiet unless explicitly @-mentioned.
EXTRA_HOME_CHANNELS: set[str] = {
    "C0BGP29BXPW",  # #fixes
    "C0BGP214VGU",  # #infra
    "C0BCDRTHUC9",  # #main
    "C0BGG2BGSTF",  # #side-projects
}
HOME_CHANNELS: frozenset[str] = frozenset({CLAUDE_CHANNEL_ID, *EXTRA_HOME_CHANNELS})

STATE_FILE = Path(__file__).resolve().parent / "sessions.json"
SLACK_CHUNK = 3500  # Slack soft-splits text above ~4000 chars

app = AsyncApp(token=BOT_TOKEN)
BOT_USER_ID: str | None = None  # this bot's Slack user id; set in main() via auth.test


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
    mode: str = "default"  # "default" routes every tool through can_use_tool
    model: str | None = None
    busy: bool = False
    session_id: str | None = None  # captured from first SystemMessage
    pending_prompts: deque = field(default_factory=deque)


sessions: dict[str, Session] = {}  # keyed by thread_ts


def make_client(
    cwd: str, mode: str, *, channel_id: str, thread_ts: str,
    resume_session_id: str | None = None, model: str | None = None,
) -> ClaudeSDKClient:
    opts = ClaudeAgentOptions(
        cwd=cwd,
        permission_mode=mode,
        can_use_tool=make_can_use_tool(channel_id, thread_ts),
        # Enable Claude Code Artifacts. The bundled CLI turns artifacts OFF by
        # default for SDK entrypoints (CLAUDE_CODE_ENTRYPOINT=sdk-py); setting
        # CLAUDE_CODE_ARTIFACT truthy overrides that sdk-default-off gate.
        # AUTO_OPEN=0 stops the headless box from trying to launch a browser —
        # the claude.ai URL is still printed and streams back to Slack.
        env={
            "CLAUDE_CODE_ARTIFACT": "1",
            "CLAUDE_CODE_ARTIFACT_AUTO_OPEN": "0",
        },
    )
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


def is_owner(event: dict) -> bool:
    """Only the owner may drive the bot — in ANY channel. The bot runs Claude
    Code with full host access, so this allowlist is load-bearing."""
    return event.get("user") == ALLOWED_USER_ID


def mentions_bot(text: str) -> bool:
    """True if the message @-mentions this bot (`<@BOT_USER_ID>`)."""
    return bool(BOT_USER_ID) and f"<@{BOT_USER_ID}>" in text


def strip_mention(text: str) -> str:
    """Remove any @-mention of this bot and collapse whitespace. The mention
    token is never meaningful to Claude, so strip it from every prompt."""
    if not BOT_USER_ID:
        return text.strip()
    cleaned = re.sub(rf"<@{re.escape(BOT_USER_ID)}>", "", text)
    return re.sub(r"\s+", " ", cleaned).strip()


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


# ---------- permission policy (allow-by-default, gate the dangerous) ----------
#
# Sessions run in "default" permission mode with `can_use_tool` as the single
# arbiter. Everything is allowed SILENTLY except a small set of irreversible
# actions, which get ✅/❌ buttons in the session thread. This matches how the
# owner actually works (approve ~everything) while still gating the handful of
# commands a reflexive phone-tap would regret. The gate FAILS CLOSED: if we
# can't reach the owner (Slack post fails), a dangerous action is denied.

DEVELOPER_ROOT = os.path.realpath(os.path.expanduser("~/Developer"))
_EDIT_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}

# Bash command patterns needing approval. Matched case-insensitively (re.search)
# against the command string. This list IS the security policy — keep it tight
# and readable; edit freely. Ordinary git push, gradle tests, reads, edits
# inside ~/Developer, etc. are intentionally NOT here — they run silently.
_DANGER_BASH_PATTERNS: list[tuple[str, str]] = [
    (r"\brm\b.*(-\w*r|--recursive)", "recursive delete (rm -r)"),
    (r"\bgit\s+push\b.*(--force|--force-with-lease|\s-\w*f)", "git force-push"),
    (r"\bgit\s+push\b.*(--delete|\s:\S)", "delete a remote branch"),
    (r"\bgit\s+reset\s+--hard\b", "git reset --hard (discards changes)"),
    (r"\bgit\s+clean\b.*-\w*f", "git clean (deletes untracked files)"),
    (r"\bgit\s+branch\b.*\s-D\b", "force-delete a branch"),
    (r"\bgit\s+tag\b.*\s-d\b", "delete a tag"),
    (r"\bsudo\b", "sudo (root access)"),
    (r"\brailway\s+(up|redeploy|down)\b", "railway deploy/teardown"),
    (r"migrat\w*.*(prod|production)|(prod|production).*migrat", "prod migration"),
    (r"\b(drop|truncate)\s+(table|database|schema)\b", "destructive SQL"),
]


def danger_match(tool: str, inp: dict | None) -> str | None:
    """Return a human reason if this tool call is gated, else None.

    Pure function — this is the unit-tested core of the policy. Bash commands
    are matched against `_DANGER_BASH_PATTERNS`; file writes are gated only when
    they target an ABSOLUTE path outside ~/Developer (relative paths, which
    Claude rarely emits for writes, are allowed to avoid false gating)."""
    inp = inp or {}
    if tool == "Bash":
        cmd = str(inp.get("command") or "")
        for pattern, reason in _DANGER_BASH_PATTERNS:
            if re.search(pattern, cmd, re.IGNORECASE):
                return reason
        return None
    if tool in _EDIT_TOOLS:
        path = inp.get("file_path") or inp.get("notebook_path") or ""
        if path and os.path.isabs(path):
            real = os.path.realpath(path)
            if real != DEVELOPER_ROOT and not real.startswith(DEVELOPER_ROOT + os.sep):
                return f"write outside ~/Developer ({path})"
    return None


# ---------- approval round-trip (Slack buttons ⇄ asyncio.Future) ----------

# tool_use_id -> Future[bool]; resolved True/False by the Bolt action handlers.
pending_approvals: dict[str, asyncio.Future] = {}


def _approval_blocks(title: str, reason: str, key: str) -> list[dict]:
    return [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"🔒 *Approval needed* — {reason}\n{title}"},
        },
        {
            "type": "actions",
            "block_id": f"approval:{key}",
            "elements": [
                {"type": "button", "action_id": "approve", "style": "primary",
                 "text": {"type": "plain_text", "text": "✅ Approve"}, "value": key},
                {"type": "button", "action_id": "deny", "style": "danger",
                 "text": {"type": "plain_text", "text": "❌ Deny"}, "value": key},
            ],
        },
    ]


async def request_approval(channel: str, thread_ts: str, tool: str, inp: dict,
                           ctx: ToolPermissionContext, reason: str):
    """Post ✅/❌ buttons and block until the owner taps. Fails CLOSED."""
    key = ctx.tool_use_id or f"{tool}:{id(inp)}"
    title = ctx.title or friendly_verb(tool, inp)
    fut: asyncio.Future = asyncio.get_running_loop().create_future()
    pending_approvals[key] = fut
    try:
        posted = await app.client.chat_postMessage(
            channel=channel, thread_ts=thread_ts,
            text=f"🔒 approval needed: {reason}",
            blocks=_approval_blocks(title, reason, key),
        )
    except Exception as e:
        pending_approvals.pop(key, None)
        log.warning("approval post failed (%s); denying %s", e, tool)
        return PermissionResultDeny(
            message=f"Could not reach owner for approval; denied ({reason})."
        )
    try:
        approved = await fut
    finally:
        pending_approvals.pop(key, None)
    verdict = "✅ approved" if approved else "❌ denied"
    try:
        await app.client.chat_update(
            channel=channel, ts=posted["ts"], text=f"{verdict}: {reason}",
            blocks=[{"type": "section", "text": {"type": "mrkdwn",
                     "text": f"{verdict} — {reason}\n{title}"}}],
        )
    except Exception:
        pass
    if approved:
        return PermissionResultAllow()
    return PermissionResultDeny(message=f"Denied by owner ({reason}).")


def make_can_use_tool(channel_id: str, thread_ts: str):
    """Build the per-session permission callback bound to its Slack thread."""
    async def can_use_tool(tool: str, inp: dict, ctx: ToolPermissionContext):
        reason = danger_match(tool, inp)
        if reason is None:
            return PermissionResultAllow()
        return await request_approval(channel_id, thread_ts, tool, inp, ctx, reason)
    return can_use_tool


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
    status = StatusBar(client, sess.channel_id, sess.thread_ts)
    await status.start()
    streamed = False
    started = time.monotonic()
    seen_blank = False  # SDK emits a blank AssistantMessage before each genuine response
    try:
        await sess.client.query(text)
        async for msg in sess.client.receive_response():
            await capture_session_id(msg, sess)
            if isinstance(msg, AssistantMessage):
                # A "blank" AssistantMessage (no text, no tool use) is the SDK's
                # streaming placeholder that precedes every genuinely new response.
                has_text = any(
                    isinstance(b, TextBlock) and b.text.strip() for b in msg.content
                )
                has_tool = any(isinstance(b, ToolUseBlock) for b in msg.content)
                if not has_text and not has_tool:
                    seen_blank = True
                for block in msg.content:
                    if isinstance(block, TextBlock) and block.text.strip():
                        # Dangling-tail guard (ported from claude-tg-bot bot.py:2060).
                        # Text that arrives BEFORE any blank placeholder, within 2s of
                        # turn start, is buffered stdout from the PREVIOUS turn's
                        # subprocess continuation — not a live response to this prompt.
                        # Absorb it silently so it isn't posted as this message's reply
                        # (the "reply is to the message above" bug).
                        if not seen_blank and (time.monotonic() - started) < 2.0:
                            log.warning(
                                "DANGLING-SKIP %s: absorbing buffered subprocess tail: %r",
                                sess.name, block.text[:60],
                            )
                            break  # skip the rest of this AssistantMessage
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
        sess.mode = "default"
        try:
            await sess.client.set_permission_mode("default")
            save_state()
            msg = "⚡ auto mode — allow-by-default, dangerous actions still gated"
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


async def pin_root(client, channel: str, ts: str) -> None:
    """Pin a session's raw root message so it shows in the channel's pinned
    panel — the native live-session list. Idempotent; ignores already-pinned."""
    try:
        await client.pins_add(channel=channel, timestamp=ts)
    except Exception as e:
        log.debug("pin_root failed (already pinned?): %s", e)


async def unpin_root(client, channel: str, ts: str) -> None:
    try:
        await client.pins_remove(channel=channel, timestamp=ts)
    except Exception as e:
        log.debug("unpin_root failed: %s", e)


# ---------- event handlers ----------


@app.event("message")
async def on_message(event, client):
    # ignore edits/joins/echoes and anyone but the owner; dispatch real work
    # to a background task so a long turn never blocks other sessions.
    if event.get("subtype") or event.get("bot_id"):
        return
    if not is_owner(event):
        if event.get("user") and event.get("channel") in HOME_CHANNELS:
            log.info("ignoring message from non-allowed user %s", event.get("user"))
        return
    asyncio.create_task(_route(client, event))


async def _route(client, event: dict) -> None:
    try:
        channel = event["channel"]
        is_home = channel in HOME_CHANNELS
        raw = event.get("text") or ""
        text = strip_mention(raw)  # mention token is never meaningful to Claude
        thread_ts = event.get("thread_ts")
        ts = event["ts"]

        # thread reply to a session we own → continue it, in ANY channel, with
        # no @-mention required (being in the thread already scopes the intent).
        if thread_ts and thread_ts in sessions:
            sess = sessions[thread_ts]
            if text.startswith("."):
                await handle_dot_command(sess, text, client)
            else:
                await drive_session(client, sess, text)
            return

        if thread_ts:  # reply in a thread we don't own — ignore
            return

        # top-level message → maybe start a new session. In the home channel any
        # message starts one; in every other channel an explicit @-mention is
        # required so the bot stays silent in shared channels unless summoned.
        if not is_home:
            if not mentions_bot(raw):
                return
            if not text:  # mentioned with no instruction → stay quiet, don't spin up
                return

        if not text:
            return

        name = slugify(text)
        sess = Session(
            name=name,
            cwd=DEFAULT_CWD,
            client=make_client(
                DEFAULT_CWD, "default", channel_id=channel, thread_ts=ts,
            ),
            channel_id=channel,
            thread_ts=ts,
        )
        await sess.client.connect()
        sessions[ts] = sess
        log.info(
            "created session %s rooted at thread_ts=%s (channel=%s, home=%s)",
            name, ts, channel, is_home,
        )
        await pin_root(client, channel, ts)
        await drive_session(client, sess, text)
    except Exception:
        log.exception("error processing message")


@app.event("app_mention")
async def on_mention(event, client):
    # No-op: every @-mention in a channel the bot belongs to ALSO arrives as a
    # `message` event, which is where routing happens (it carries thread_ts and
    # unifies home + other-channel handling). Acting here too would double-
    # process. The bot must be a channel member to operate there (chat:write
    # requires membership), so a mention without membership can't be served
    # anyway — invite the bot first. Handler kept to suppress "unhandled" warns.
    return


CLOSE_REACTIONS = {"white_check_mark", "heavy_check_mark"}


@app.event("reaction_added")
async def on_reaction(event, client):
    # ✅ on a session's root message → unpin it and tear the session down.
    # reaction_added carries channel under item (not top-level), so we check
    # reactor + channel + emoji explicitly rather than via is_owner().
    if event.get("user") != ALLOWED_USER_ID:
        return
    if event.get("reaction") not in CLOSE_REACTIONS:
        return
    item = event.get("item") or {}
    if item.get("channel") != CLAUDE_CHANNEL_ID:
        return
    ts = item.get("ts")
    sess = sessions.pop(ts, None)
    if sess is None:  # reaction on some other message — ignore
        return

    await unpin_root(client, item["channel"], ts)
    try:
        await sess.client.disconnect()
    except Exception as e:
        log.warning("disconnect failed closing %s: %s", sess.name, e)
    save_state()
    log.info("closed session %s via ✅ (thread_ts=%s)", sess.name, ts)
    try:
        await client.chat_postMessage(
            channel=item["channel"], thread_ts=ts, text="✅ session closed",
        )
    except Exception:
        pass


@app.command("/list")
async def cmd_list(ack, client):
    # the pinned panel IS the session list; /list just re-pins any active
    # session whose pin got lost (e.g. manually unpinned).
    await ack()
    for ts, s in sessions.items():
        await pin_root(client, s.channel_id, ts)


# ---------- approval buttons ----------


async def _resolve_approval(body: dict, action: dict, approved: bool) -> None:
    # Only the owner's taps count — the buttons are visible to the whole channel
    # but load-bearing for host access, so gate on user id (cf. is_owner).
    if (body.get("user") or {}).get("id") != ALLOWED_USER_ID:
        return
    key = action.get("value")
    fut = pending_approvals.get(key)
    if fut and not fut.done():
        fut.set_result(approved)


@app.action("approve")
async def on_approve(ack, body, action):
    await ack()
    await _resolve_approval(body, action, approved=True)


@app.action("deny")
async def on_deny(ack, body, action):
    await ack()
    await _resolve_approval(body, action, approved=False)


# ---------- startup ----------


async def restore_sessions() -> None:
    """Rebuild sessions from sessions.json, resuming each Claude client by its
    persisted session_id so conversations survive a bot restart."""
    data = load_state()
    for ts, sd in data.get("sessions", {}).items():
        try:
            # legacy "acceptEdits" sessions migrate to the guardrail ("default")
            mode = sd.get("mode") or "default"
            if mode == "acceptEdits":
                mode = "default"
            c = make_client(
                sd["cwd"], mode,
                channel_id=sd["channel_id"], thread_ts=ts,
                resume_session_id=sd.get("session_id"), model=sd.get("model"),
            )
            await c.connect()
            sessions[ts] = Session(
                name=sd["name"],
                cwd=sd["cwd"],
                client=c,
                channel_id=sd["channel_id"],
                thread_ts=ts,
                mode=mode,
                model=sd.get("model"),
                session_id=sd.get("session_id"),
            )
        except Exception:
            log.exception("failed to restore session %s", sd.get("name"))
    log.info("restored %d sessions", len(sessions))


async def main() -> None:
    global BOT_USER_ID
    try:
        BOT_USER_ID = (await app.client.auth_test())["user_id"]
    except Exception:
        log.exception(
            "auth_test failed; @-mention gating in other channels is disabled"
        )
    log.info(
        "starting claude-slack-bot; channel=%s user=%s bot=%s cwd=%s",
        CLAUDE_CHANNEL_ID, ALLOWED_USER_ID, BOT_USER_ID, DEFAULT_CWD,
    )
    await restore_sessions()
    handler = AsyncSocketModeHandler(app, APP_TOKEN)
    await handler.start_async()


if __name__ == "__main__":
    asyncio.run(main())
