import os
import pathlib
import sys

# bot.py reads required Slack env vars at import time; set dummies so the
# pure policy function can be imported and tested without a real .env.
os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-test")
os.environ.setdefault("SLACK_APP_TOKEN", "xapp-test")
os.environ.setdefault("ALLOWED_USER_ID", "Utest")
os.environ.setdefault("CLAUDE_CHANNEL_ID", "Ctest")

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from bot import DEVELOPER_ROOT, danger_match  # noqa: E402


def _bash(cmd: str):
    return danger_match("Bash", {"command": cmd})


# ---- allowed silently (the common case) ----

def test_gradle_test_allowed():
    assert _bash("./gradlew test") is None
    assert _bash("gradle test --info") is None


def test_read_only_git_allowed():
    assert _bash("git status") is None
    assert _bash("git diff HEAD~1") is None
    assert _bash("git log --oneline -10") is None


def test_normal_push_allowed():
    assert _bash("git push origin main") is None


def test_plain_rm_single_file_allowed():
    # non-recursive delete of one file is low-regret; not gated
    assert _bash("rm build.log") is None


def test_npm_not_matched_as_rm():
    assert _bash("npm run build") is None


# ---- gated (the rare, irreversible case) ----

def test_force_push_gated():
    assert _bash("git push --force") is not None
    assert _bash("git push -f origin main") is not None
    assert _bash("git push --force-with-lease origin feat") is not None


def test_remote_branch_delete_gated():
    assert _bash("git push origin --delete old-branch") is not None
    assert _bash("git push origin :old-branch") is not None


def test_recursive_delete_gated():
    assert _bash("rm -rf build/") is not None
    assert _bash("rm -r node_modules") is not None
    assert _bash("find . -name x -exec rm -rf {} +") is not None


def test_hard_reset_gated():
    assert _bash("git reset --hard origin/main") is not None


def test_git_clean_gated():
    assert _bash("git clean -fd") is not None


def test_branch_and_tag_delete_gated():
    assert _bash("git branch -D feature") is not None
    assert _bash("git tag -d v1.2.3") is not None


def test_sudo_gated():
    assert _bash("sudo systemctl restart nginx") is not None


def test_railway_deploy_gated():
    assert _bash("railway up") is not None
    assert _bash("railway redeploy") is not None


def test_prod_migration_gated():
    assert _bash("npx prisma migrate deploy --env production") is not None


def test_destructive_sql_gated():
    assert _bash("psql -c 'DROP TABLE users'") is not None
    assert _bash("psql -c 'truncate table events'") is not None


# ---- file-write scoping ----

def test_edit_inside_developer_tree_allowed():
    path = os.path.join(DEVELOPER_ROOT, "some", "repo", "file.py")
    assert danger_match("Edit", {"file_path": path}) is None
    assert danger_match("Write", {"file_path": path}) is None


def test_write_outside_developer_tree_gated():
    assert danger_match("Write", {"file_path": "/etc/hosts"}) is not None


def test_relative_write_not_gated():
    # Claude rarely emits relative write paths; allow to avoid false gating
    assert danger_match("Write", {"file_path": "notes.txt"}) is None
