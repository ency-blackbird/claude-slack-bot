#!/bin/bash
# Bounce the Slack bot. Works whether you call it interactively from a
# terminal or from inside the bot's own SDK process (where the parent would
# otherwise SIGHUP the restart subshell).
#
# Usage:
#   ./restart.sh         # immediate
#   ./restart.sh 6       # wait 6s before bouncing (lets in-flight turns finish)
#
# Paths are derived from the script's own location so this works on any
# install — no edits required.

set -u
DELAY="${1:-0}"
BOT_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON="$BOT_DIR/.venv/bin/python"
LOG="${CLAUDE_SLACK_LOG:-$BOT_DIR/bot.log}"

# If a launchd agent supervises the bot, delegate to it. `kickstart -k` kills
# and restarts as ONE managed process — doing our own pkill + relaunch would
# race launchd's respawn and leave two bots, both holding Socket Mode
# connections and double-processing every Slack event. Falls through to the
# manual path when no agent is loaded (e.g. Linux, or before install).
LAUNCHD_LABEL="com.$(id -un).claude-slack-bot"
if command -v launchctl >/dev/null 2>&1 && \
   launchctl print "gui/$(id -u)/$LAUNCHD_LABEL" >/dev/null 2>&1; then
    (
        nohup bash -c "sleep $DELAY; launchctl kickstart -k 'gui/$(id -u)/$LAUNCHD_LABEL'" \
            </dev/null >/dev/null 2>&1 &
    )
    echo "restart scheduled via launchd (delay=${DELAY}s, label=$LAUNCHD_LABEL) · log: $LOG"
    exit 0
fi

# Detach into its own session so SIGHUP from the parent shell never reaches us.
# The ( cmd & ) pattern forks twice — the inner backgrounded subshell gets
# orphaned and adopted by launchd (PID 1 on mac) / init (Linux).
(
    nohup bash -c "
        sleep $DELAY
        pkill -f '$BOT_DIR/bot.py' 2>/dev/null
        pkill -f 'Python bot.py' 2>/dev/null
        sleep 2
        cd '$BOT_DIR'
        exec '$PYTHON' bot.py >> '$LOG' 2>&1
    " </dev/null >/dev/null 2>&1 &
)
echo "restart scheduled (delay=${DELAY}s) · log: $LOG"
