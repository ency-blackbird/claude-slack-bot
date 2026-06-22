import os
import pathlib
import sys

# bot.py reads required Slack env vars at import time; set dummies so the
# pure formatter can be imported and tested without a real .env.
os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-test")
os.environ.setdefault("SLACK_APP_TOKEN", "xapp-test")
os.environ.setdefault("ALLOWED_USER_ID", "Utest")
os.environ.setdefault("CLAUDE_CHANNEL_ID", "Ctest")

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from bot import to_mrkdwn  # noqa: E402


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
    assert to_mrkdwn("```\n**literal**\n```") == "```\n**literal**\n```"


def test_bullet_dash_preserved():
    assert to_mrkdwn("- item") == "- item"
