from functools import wraps
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
    from datetime import datetime, timedelta, timezone
    import hashlib
    if __package__:
        from . import db
    else:
        import db

    timestamp = time.time() if now is None else now
    instant = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=timestamp)
    cutoff = (instant - timedelta(days=1)).isoformat()
    created = instant.isoformat()
    if workspace_id is not None and db.backend() == "sqlite":
        db._make_workspace(workspace_id)
    with db._workspace_connection() as connection:
        if connection.dialect == "postgres":
            key = int.from_bytes(hashlib.sha256(f"usage:{workspace_id}:{kind}".encode()).digest()[:8], "big", signed=True)
            connection._run("SELECT pg_advisory_xact_lock(?)", (key,))
        connection._run("DELETE FROM usage_events WHERE created_at <= ?", (cutoff,))
        condition = "workspace_id IS NULL" if workspace_id is None else "workspace_id = ?"
        params = (kind, cutoff) if workspace_id is None else (kind, cutoff, workspace_id)
        used = connection._run("SELECT COUNT(*) AS count FROM usage_events WHERE kind = ? AND created_at > ? AND " + condition, params).fetchone()["count"]
        if used >= limit:
            raise UsageLimitError("The demo's usage limit has been reached. Please try again later.")
        connection._run("INSERT INTO usage_events(workspace_id, kind, created_at) VALUES (?, ?, ?)", (workspace_id, kind, created))


def reserve_workspace_action(kind, now=None):
    limits = {"chat": config.WORKSPACE_CHAT_LIMIT, "tool": config.WORKSPACE_TOOL_LIMIT, "upload": config.WORKSPACE_UPLOAD_LIMIT}
    if kind not in limits:
        raise ValueError("Unknown action category.")
    storage = get_storage()
    if storage.workspace_id is not None and storage.workspace_id != "demo-template":
        _reserve(kind, storage.workspace_id, limits[kind], now)


def reserve_api_action(now=None):
    _reserve("api", None, config.GLOBAL_API_ACTION_LIMIT, now)


def guard_api_call(function):
    @wraps(function)
    def guarded(*args, **kwargs):
        config.check_request()
        reserve_api_action()
        result = function(*args, **kwargs)
        try:
            config.check_request()
        except BaseException:
            close = getattr(result, "close", None)
            if callable(close):
                close()
            raise
        return result
    return guarded
