from functools import wraps
import sqlite3
import time

if __package__:
    from . import config
    from .workspaces import get_storage
else:
    import config
    from workspaces import get_storage


class UsageLimitError(RuntimeError):
    public_message = True


def _reserve(kind, workspace_id, limit, now):
    now = time.time() if now is None else now
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(config.DATA_DIR / "usage.db", timeout=30)
    try:
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("CREATE TABLE IF NOT EXISTS usage_events (workspace_id TEXT, kind TEXT NOT NULL, occurred_at REAL NOT NULL)")
            connection.execute("CREATE INDEX IF NOT EXISTS usage_lookup ON usage_events(kind, workspace_id, occurred_at)")
            connection.execute("DELETE FROM usage_events WHERE occurred_at <= ?", (now - 86400,))
            used = connection.execute(
                "SELECT COUNT(*) FROM usage_events WHERE kind = ? AND workspace_id IS ? AND occurred_at > ?",
                (kind, workspace_id, now - 86400),
            ).fetchone()[0]
            if used >= limit:
                raise UsageLimitError("The demo's usage limit has been reached. Please try again later.")
            connection.execute("INSERT INTO usage_events(workspace_id, kind, occurred_at) VALUES (?, ?, ?)", (workspace_id, kind, now))
    finally:
        connection.close()


def reserve_workspace_action(kind, now=None):
    limits = {"chat": config.WORKSPACE_CHAT_LIMIT, "tool": config.WORKSPACE_TOOL_LIMIT, "upload": config.WORKSPACE_UPLOAD_LIMIT}
    if kind not in limits:
        raise ValueError("Unknown action category.")
    storage = get_storage()
    if storage.workspace_id is not None:
        _reserve(kind, storage.workspace_id, limits[kind], now)


def reserve_api_action(now=None):
    _reserve("api", None, config.GLOBAL_API_ACTION_LIMIT, now)


def guard_api_call(function):
    @wraps(function)
    def guarded(*args, **kwargs):
        reserve_api_action()
        return function(*args, **kwargs)
    return guarded
